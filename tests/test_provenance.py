"""Provenance chain tests on the shared sample graph (Sec. 14)."""

import datetime
from decimal import Decimal

import pytest

from utxoproof.db import open_memory_db
from utxoproof.kyc import create_sample_graph, propagate_graph, seed_source_kyc
from utxoproof.provenance import (
    build_provenance_chain,
    build_saga,
    classify_event,
    saga_source_share,
)

CURVE = {
    datetime.date(2023, 1, 1): Decimal("20000"),
    datetime.date(2023, 1, 2): Decimal("21000"),
    datetime.date(2023, 2, 1): Decimal("25000"),
    datetime.date(2023, 3, 1): Decimal("30000"),
}


def _price(day: datetime.date) -> Decimal:
    return CURVE[day]


def _db():
    db = create_sample_graph()
    seed_source_kyc(db)
    propagate_graph(db)
    return db


def test_chain_follows_largest_parent_with_deterministic_ties() -> None:
    # C merges A:0 + B:0 (1.0 BTC each); tie goes to lowest input_index (A).
    steps = build_provenance_chain("D", 0, _db(), _price)
    assert [(s.txid, s.vout) for s in steps] == [("A", 0), ("C", 0), ("D", 0)]
    assert [s.step_number for s in steps] == [1, 2, 3]


def test_events_valuations_and_mixing() -> None:
    steps = build_provenance_chain("D", 0, _db(), _price)
    by_id = {(s.txid, s.vout): s for s in steps}
    assert by_id[("A", 0)].event_description.startswith("Exchange purchase")
    assert by_id[("C", 0)].event_description == "Consolidation (2 inputs merged)"
    assert by_id[("D", 0)].event_description == "Self-transfer (simple move between wallets)"
    assert by_id[("C", 0)].mixing_event is True
    assert by_id[("D", 0)].mixing_event is False
    assert by_id[("A", 0)].eur_value == Decimal("1") * Decimal("20000")
    assert by_id[("C", 0)].kyc_status == "mixed"
    assert by_id[("C", 0)].sibling_utxos == [("C", 1)]
    assert by_id[("C", 0)].parent_utxos == [("A", 0), ("B", 0)]


def test_depth_cap() -> None:
    steps = build_provenance_chain("D", 0, _db(), _price, max_depth=2)
    assert [(s.txid, s.vout) for s in steps] == [("C", 0), ("D", 0)]


def test_saga_walks_all_parents_with_proportional_shares() -> None:
    nodes, truncated = build_saga("D", 0, _db(), _price)
    by_id = {(n.txid, n.vout): n for n in nodes}

    # C merges A:0 + B:0 (1.0 BTC each); D spends C:0. The old chain hid B.
    assert [n.txid for n in nodes] == ["A", "B", "C", "D"]
    assert by_id[("A", 0)].share == Decimal("0.5")
    assert by_id[("B", 0)].share == Decimal("0.5")
    assert by_id[("C", 0)].share == Decimal("1")
    assert by_id[("D", 0)].share == Decimal("1")
    assert [by_id[(txid, 0)].depth for txid in "ABCD"] == [2, 2, 1, 0]

    assert by_id[("A", 0)].is_source is True
    assert by_id[("B", 0)].is_source is True
    assert by_id[("C", 0)].is_source is False
    assert by_id[("C", 0)].parent_utxos == (("A", 0), ("B", 0))
    assert by_id[("C", 0)].sibling_utxos == (("C", 1),)
    assert by_id[("A", 0)].event == "Exchange purchase"
    assert by_id[("B", 0)].event == "Coinbase reward (mining)"
    assert by_id[("C", 0)].event == "Consolidation (2 inputs merged)"
    assert by_id[("C", 0)].mixing_event is True
    assert by_id[("D", 0)].mixing_event is False
    assert saga_source_share(nodes) == Decimal("1")
    assert truncated == Decimal("0")

    # The share is of the target: A's 50% of D:0 (1.5 BTC) is 0.75 BTC.
    assert by_id[("A", 0)].eur_value == Decimal("20000")
    assert by_id[("A", 0)].share * Decimal("1.5") == Decimal("0.75")


def test_saga_depth_cap_reports_truncated_share() -> None:
    nodes, truncated = build_saga("D", 0, _db(), _price, max_depth=2)
    assert [(n.txid, n.vout) for n in nodes] == [("C", 0), ("D", 0)]
    assert truncated == Decimal("1")
    assert saga_source_share(nodes) == Decimal("0")


def test_saga_node_cap_keeps_largest_shares_and_truncates_the_rest() -> None:
    nodes, truncated = build_saga("D", 0, _db(), _price, max_nodes=3)
    assert [(n.txid, n.vout) for n in nodes] == [("A", 0), ("C", 0), ("D", 0)]
    assert truncated == Decimal("0.5")
    assert saga_source_share(nodes) == Decimal("0.5")

    full, full_truncated = build_saga("D", 0, _db(), _price, max_nodes=2)
    assert [(n.txid, n.vout) for n in full] == [("C", 0), ("D", 0)]
    assert full_truncated == Decimal("1")
    assert saga_source_share(full) == Decimal("0")


def test_saga_fanout_fee_and_siblings() -> None:
    db = _db()
    db.execute("INSERT INTO transactions (txid, block_time) VALUES ('E', '2023-03-01')")
    db.execute("INSERT INTO tx_outputs (txid, vout, value_sat) VALUES ('E', 0, 30000000)")
    db.execute("INSERT INTO tx_outputs (txid, vout, value_sat) VALUES ('E', 1, 10000000)")
    db.execute(
        "INSERT INTO tx_inputs (txid, input_index, prev_txid, prev_vout) VALUES ('E', 0, 'C', 1)"
    )

    nodes, truncated = build_saga("E", 1, db, _price)
    by_id = {(n.txid, n.vout): n for n in nodes}
    assert [n.txid for n in nodes] == ["A", "B", "C", "E"]
    assert by_id[("E", 1)].share == Decimal("1")
    assert by_id[("E", 1)].sibling_utxos == (("E", 0),)
    assert by_id[("E", 1)].event == "Spend (payment + change)"
    assert by_id[("E", 1)].fee_sat == 10_000_000
    assert by_id[("C", 1)].share == Decimal("1")
    assert by_id[("A", 0)].share == Decimal("0.5")
    assert by_id[("B", 0)].share == Decimal("0.5")
    assert truncated == Decimal("0")
    assert saga_source_share(nodes) == Decimal("1")


def test_saga_sums_shares_for_a_repeated_parent() -> None:
    db = open_memory_db()
    db.execute("INSERT INTO transactions (txid, block_time) VALUES ('P', '2023-01-01')")
    db.execute("INSERT INTO tx_outputs (txid, vout, value_sat) VALUES ('P', 0, 100000000)")
    db.execute("INSERT INTO transactions (txid, block_time) VALUES ('X', '2023-02-01')")
    db.execute("INSERT INTO tx_outputs (txid, vout, value_sat) VALUES ('X', 0, 100000000)")
    for index in (0, 1):
        db.execute(
            "INSERT INTO tx_inputs (txid, input_index, prev_txid, prev_vout) "
            "VALUES ('X', ?, 'P', 0)",
            (index,),
        )

    nodes, truncated = build_saga("X", 0, db, _price)
    by_id = {(n.txid, n.vout): n for n in nodes}
    assert by_id[("P", 0)].share == Decimal("1")
    assert truncated == Decimal("0")
    assert saga_source_share(nodes) == Decimal("1")


def test_saga_missing_parent_is_truncated() -> None:
    db = open_memory_db()
    db.execute("INSERT INTO transactions (txid, block_time) VALUES ('X', '2023-01-01')")
    db.execute("INSERT INTO tx_outputs (txid, vout, value_sat) VALUES ('X', 0, 100000000)")
    db.execute(
        "INSERT INTO tx_inputs (txid, input_index, prev_txid, prev_vout) VALUES ('X', 0, 'M', 0)"
    )

    nodes, truncated = build_saga("X", 0, db, _price)
    assert [(n.txid, n.vout) for n in nodes] == [("X", 0)]
    assert nodes[0].is_source is False
    assert "missing parent M:0" in nodes[0].evidence
    assert truncated == Decimal("1")
    assert saga_source_share(nodes) == Decimal("0")


def test_saga_corrupt_cycle_is_truncated_not_looped() -> None:
    db = open_memory_db()
    db.execute("INSERT INTO transactions (txid, block_time) VALUES ('X', '2023-01-01')")
    db.execute("INSERT INTO tx_outputs (txid, vout, value_sat) VALUES ('X', 0, 100000000)")
    db.execute(
        "INSERT INTO tx_inputs (txid, input_index, prev_txid, prev_vout) VALUES ('X', 0, 'X', 0)"
    )

    nodes, truncated = build_saga("X", 0, db, _price)
    assert [(n.txid, n.vout) for n in nodes] == [("X", 0)]
    assert truncated == Decimal("1")
    assert saga_source_share(nodes) == Decimal("0")


def test_saga_inputless_receipt_is_not_assumed_mining() -> None:
    db = open_memory_db()
    for txid, is_coinbase in (("R", 0), ("CB", 1)):
        db.execute(
            "INSERT INTO transactions (txid, block_time, is_coinbase) VALUES (?, '2023-01-01', ?)",
            (txid, is_coinbase),
        )
        db.execute(
            "INSERT INTO tx_outputs (txid, vout, value_sat) VALUES (?, 0, 100000000)",
            (txid,),
        )

    receipt, _ = build_saga("R", 0, db, _price)
    coinbase, _ = build_saga("CB", 0, db, _price)
    assert receipt[0].event == "External receipt (no on-chain parent)"
    assert coinbase[0].event == "Coinbase reward (mining)"


def test_saga_rejects_unknown_target_and_invalid_caps() -> None:
    with pytest.raises(ValueError, match="unknown UTXO"):
        build_saga("missing", 0, _db(), _price)
    with pytest.raises(ValueError, match="max_depth"):
        build_saga("D", 0, _db(), _price, max_depth=0)
    with pytest.raises(ValueError, match="max_nodes"):
        build_saga("D", 0, _db(), _price, max_nodes=0)


def test_classify_event_matrix() -> None:
    assert classify_event(None, None, 0, 1) == "Coinbase reward (mining)"
    assert classify_event("exchange_purchase", None, 0, 1) == "Exchange purchase"
    assert classify_event("exchange_purchase", "Kraken #1", 2, 2) == "Exchange purchase (Kraken #1)"
    assert classify_event(None, None, 1, 3) == "Fan-out (3 outputs created)"
    assert classify_event(None, None, 1, 2) == "Spend (payment + change)"
    assert classify_event(None, None, 3, 5) == "On-chain transaction (3 inputs, 5 outputs)"


def test_provenance_page_and_cli(tmp_path) -> None:
    import sqlite3

    from utxoproof.cli import main
    from utxoproof.kyc import create_sample_graph
    from utxoproof.reports import write_provenance_page

    target = write_provenance_page(
        create_sample_graph(),
        "D",
        0,
        _price,
        Decimal("40000"),
        datetime.date(2024, 6, 1),
        tmp_path,
    )
    html = target.read_text(encoding="utf-8")
    assert "Consolidation (2 inputs merged)" in html
    assert 'class="mixing"' in html
    assert "mixed" in html  # D:0 KYC status in the chain table
    assert "privacy_risk" not in html and "speculation_tainted" not in html

    db_path = tmp_path / "prov.db"
    source = create_sample_graph()
    dest = sqlite3.connect(str(db_path))
    source.backup(dest)
    dest.close()
    assert (
        main(
            [
                "provenance",
                "D:0",
                "--db",
                str(db_path),
                "--price",
                "40000",
                "--as-of",
                "2024-06-01",
                "--out",
                str(tmp_path),
            ]
        )
        == 0
    )
    assert (tmp_path / "provenance_D_0.html").exists()


def test_provenance_prologue_names_both_proofs(tmp_path) -> None:
    import datetime
    from decimal import Decimal

    from utxoproof.kyc import create_sample_graph
    from utxoproof.linking import TrailCandidate
    from utxoproof.reports import write_provenance_page

    trail = [
        TrailCandidate(
            chain_txid="C",
            withdrawal_refid="QW",
            withdrawal_date="2023-02-01",
            buy_refid="R1",
            buy_date="2023-01-10",
            buy_btc="1",
            buy_fiat_eur="20000.00",
            bank=None,
            ledgers_file="ledgers.csv",
            bank_file="bank.csv",
        )
    ]
    target = write_provenance_page(
        create_sample_graph(),
        "D",
        0,
        _price,
        Decimal("40000"),
        datetime.date(2024, 6, 1),
        tmp_path,
        trail=trail,
    )
    html = target.read_text(encoding="utf-8")
    assert "Fiat funding prologue" in html
    assert "QW" in html and "R1" in html
    assert "ledgers.csv" in html and "bank.csv" in html
    assert "no bank row matched" in html


def test_provenance_prologue_absent_without_trail(tmp_path) -> None:
    import datetime
    from decimal import Decimal

    from utxoproof.kyc import create_sample_graph
    from utxoproof.reports import write_provenance_page

    target = write_provenance_page(
        create_sample_graph(),
        "D",
        0,
        _price,
        Decimal("40000"),
        datetime.date(2024, 6, 1),
        tmp_path,
    )
    assert "Fiat funding prologue" not in target.read_text(encoding="utf-8")
