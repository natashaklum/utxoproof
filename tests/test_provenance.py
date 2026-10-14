"""Provenance chain tests on the shared sample graph (Sec. 14)."""

import datetime
from decimal import Decimal

from utxoproof.kyc import create_sample_graph, propagate_graph, seed_source_kyc
from utxoproof.provenance import build_provenance_chain, classify_event

CURVE = {
    datetime.date(2023, 1, 1): Decimal("20000"),
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


def test_classify_event_matrix() -> None:
    assert classify_event(None, None, 0, 1) == "Coinbase reward (mining)"
    assert classify_event("exchange_purchase", "Kraken #1", 2, 2).startswith("Exchange")
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
    assert "privacy_risk" in html  # header advisory flags for mixed D:0

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
