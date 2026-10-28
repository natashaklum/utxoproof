"""Cross-domain saga links, persistence, and value-weighted coverage."""

from __future__ import annotations

import datetime
import sqlite3
from decimal import Decimal

from utxoproof.banks import BankRow
from utxoproof.exchange import ExchangeTx, FiatFlow
from utxoproof.kyc import create_sample_graph
from utxoproof.provenance import build_saga
from utxoproof.saga import (
    ProvenanceLink,
    SagaSources,
    allocate_trades_to_withdrawals,
    build_saga_document,
    build_saga_links,
    delete_derived_links,
    list_provenance_links,
    match_bank_to_deposits,
    match_withdrawals_to_chain,
    set_user_link,
    upsert_provenance_link,
)


def _buy(day: str, btc: str = "1", eur: str = "20000", refid: str = "T1") -> ExchangeTx:
    return ExchangeTx(
        datetime.date.fromisoformat(day),
        "BUY",
        Decimal(btc),
        Decimal(eur),
        Decimal("0"),
        refid,
        exchange="kraken",
        source_evidence=f"ledgers.csv:refid:{refid}",
    )


def _withdrawal(day: str, btc: str = "1", refid: str = "W1") -> ExchangeTx:
    return ExchangeTx(
        datetime.date.fromisoformat(day),
        "WITHDRAWAL",
        Decimal(btc),
        Decimal("0"),
        Decimal("0"),
        refid,
        exchange="kraken",
        source_evidence=f"ledgers.csv:refid:{refid}",
    )


def _deposit(day: str, eur: str = "20000", refid: str = "D1") -> FiatFlow:
    return FiatFlow(
        datetime.date.fromisoformat(day),
        "DEPOSIT",
        Decimal(eur),
        refid,
        source_evidence=f"ledgers.csv:refid:{refid}",
    )


def _bank(
    day: str,
    eur: str,
    ref: str = "D1",
    row: int = 2,
    file: str = "bank.csv",
) -> BankRow:
    return BankRow(
        datetime.date.fromisoformat(day),
        "Kraken deposit",
        Decimal(eur),
        None,
        ref,
        source_file=file,
        row_number=row,
    )


def _full_sources() -> SagaSources:
    return SagaSources(
        transactions=[_buy("2023-01-02"), _withdrawal("2023-01-03")],
        fiat_flows=[_deposit("2022-12-30")],
        bank_rows=[_bank("2022-12-30", "-20000")],
    )


def test_cross_domain_saga_has_four_layers_and_full_coverage() -> None:
    db = create_sample_graph()
    doc = build_saga_document(
        db,
        "D",
        0,
        lambda _day: Decimal("20000"),
        sources=_full_sources(),
    )

    assert doc.truncated_share == 0
    # The sample target is half exchange coins and half mining coins.
    assert doc.coverage == Decimal("0.5")
    assert doc.weakest_tier == "allocated"
    assert {link.layer for link in doc.links} == {"bank", "exchange", "chain"}
    assert len(doc.links) == 4
    bank = next(link for link in doc.links if link.layer == "bank")
    assert bank.confidence == "exact"
    assert bank.amount_eur == Decimal("20000")
    assert "bank.csv:2" in bank.evidence_ref
    chain = next(link for link in doc.links if link.layer == "chain")
    assert chain.confidence == "exact"
    assert chain.to_ref == "A:0"
    assert doc.source_coverage[0].txid == "A"
    assert doc.source_coverage[0].tier == "allocated"


def test_rebuild_preserves_user_rejection_and_removes_coverage() -> None:
    db = create_sample_graph()
    doc = build_saga_document(db, "D", 0, lambda _day: Decimal("20000"), sources=_full_sources())
    bank = next(link for link in doc.links if link.layer == "bank")
    set_user_link(db, bank, confidence="rejected", note="user says this is not the transfer")
    db.commit()

    rebuilt = build_saga_document(
        db, "D", 0, lambda _day: Decimal("20000"), sources=_full_sources()
    )
    stored = next(link for link in rebuilt.links if link.layer == "bank")
    assert stored.origin == "user"
    assert stored.confidence == "rejected"
    assert stored.note == "user says this is not the transfer"
    assert rebuilt.coverage == 0
    assert rebuilt.weakest_tier == ""


def test_rebuild_preserves_user_confirmation_and_keeps_coverage() -> None:
    db = create_sample_graph()
    doc = build_saga_document(db, "D", 0, lambda _day: Decimal("20000"), sources=_full_sources())
    bank = next(link for link in doc.links if link.layer == "bank")
    set_user_link(db, bank, confidence="exact", note="confirmed")
    db.commit()

    rebuilt = build_saga_document(
        db, "D", 0, lambda _day: Decimal("20000"), sources=_full_sources()
    )
    stored = next(link for link in rebuilt.links if link.layer == "bank")
    assert stored.origin == "user"
    assert stored.confidence == "exact"
    assert stored.note == "confirmed"
    assert rebuilt.coverage == Decimal("0.5")


def test_empty_source_rebuild_clears_stale_derived_links() -> None:
    db = create_sample_graph()
    build_saga_document(db, "D", 0, lambda _day: Decimal("20000"), sources=_full_sources())
    assert list_provenance_links(db, "D", 0)
    emptied = build_saga_document(db, "D", 0, lambda _day: Decimal("20000"), sources=SagaSources())
    assert list_provenance_links(db, "D", 0) == []
    assert emptied.coverage == 0


def test_derived_rebuild_does_not_duplicate_edges() -> None:
    db = create_sample_graph()
    for _ in range(2):
        build_saga_document(db, "D", 0, lambda _day: Decimal("20000"), sources=_full_sources())
    assert len(list_provenance_links(db, "D", 0)) == 4


def test_delete_derived_links_keeps_user_rows() -> None:
    db = create_sample_graph()
    derived = ProvenanceLink("D", 0, "bank", "a", "b", "exact", "e")
    user = ProvenanceLink("D", 0, "bank", "c", "d", "exact", "e", origin="user")
    upsert_provenance_link(db, derived)
    upsert_provenance_link(db, user)
    delete_derived_links(db, "D", 0)
    links = list_provenance_links(db, "D", 0)
    assert [(link.from_ref, link.origin) for link in links] == [("c", "user")]


def test_upsert_keeps_user_origin_when_derived_arrives_later() -> None:
    db = create_sample_graph()
    user = ProvenanceLink("D", 0, "bank", "a", "b", "rejected", "e", origin="user")
    derived = ProvenanceLink("D", 0, "bank", "a", "b", "exact", "new")
    upsert_provenance_link(db, user)
    upsert_provenance_link(db, derived)
    links = list_provenance_links(db, "D", 0)
    assert len(links) == 1
    assert links[0].origin == "user" and links[0].confidence == "rejected"


def test_bank_exact_then_approximate_and_ambiguous() -> None:
    exact, notes = match_bank_to_deposits(
        "D", 0, [_bank("2023-01-01", "-20000")], [_deposit("2023-01-01")]
    )
    assert not notes
    assert exact[0].confidence == "exact"

    approximate, notes = match_bank_to_deposits(
        "D", 0, [_bank("2023-01-01", "-20000.50")], [_deposit("2023-01-01")]
    )
    assert not notes
    assert approximate[0].confidence == "approximate"
    assert "~Δ€0.50" in approximate[0].note

    ambiguous, notes = match_bank_to_deposits(
        "D",
        0,
        [_bank("2023-01-01", "-20000", ref="unrelated")],
        [_deposit("2023-01-01", refid="D1"), _deposit("2023-01-01", refid="D2")],
    )
    assert ambiguous == []
    assert any("ambiguous" in note for note in notes)


def test_ambiguous_chain_candidates_stay_unresolved() -> None:
    db = create_sample_graph()
    # Add a second input-less source with the same amount and date as A:0.
    db.execute("INSERT INTO transactions (txid, block_time) VALUES ('A2', '2023-01-01')")
    db.execute(
        "INSERT INTO tx_outputs (txid, vout, value_sat, source_type) "
        "VALUES ('A2', 0, 100000000, 'exchange_purchase')"
    )
    db.execute(
        "INSERT INTO tx_inputs (txid, input_index, prev_txid, prev_vout) VALUES ('D', 1, 'A2', 0)"
    )
    db.execute("UPDATE tx_outputs SET spent_by_txid='D' WHERE txid='A2' AND vout=0")
    db.commit()
    nodes, _ = build_saga("D", 0, db, lambda _day: Decimal("20000"))
    links, notes = match_withdrawals_to_chain("D", 0, nodes, [_withdrawal("2023-01-01")])
    assert links == []
    assert any("chain candidates" in note for note in notes)


def test_unreliable_btc_replay_marks_candidates_not_coverage() -> None:
    txs = [
        ExchangeTx(
            datetime.date(2023, 1, 1),
            "SELL",
            Decimal("0.5"),
            Decimal("20000"),
            Decimal("0"),
            "S0",
        ),
        _buy("2023-01-02"),
        _withdrawal("2023-01-03"),
    ]
    links, notes = allocate_trades_to_withdrawals("D", 0, txs)
    assert links and all(link.confidence == "candidate" for link in links)
    assert any("unusable" in note for note in notes)


def test_parse_kraken_fiat_flows(tmp_path) -> None:
    from utxoproof.kraken_csv import parse_kraken_fiat_flows

    path = tmp_path / "ledgers.csv"
    path.write_text(
        "txid,refid,time,type,subtype,aclass,asset,amount,fee,balance\n"
        "L1,D1,2023-01-01 00:00:00,deposit,,currency,ZEUR,20000.00,0,20000\n"
        "L2,W1,2023-01-02 00:00:00,withdrawal,,currency,ZEUR,-5000.00,0,15000\n"
        "L3,T1,2023-01-03 00:00:00,trade,,currency,XXBT,1.0,0,1\n"
        "L4,T1,2023-01-03 00:00:00,trade,,currency,ZEUR,-20000.00,0,0\n",
        encoding="utf-8",
    )
    flows = parse_kraken_fiat_flows(path)
    assert [(flow.kind, flow.eur, flow.refid) for flow in flows] == [
        ("DEPOSIT", Decimal("20000.00"), "D1"),
        ("WITHDRAWAL", Decimal("5000.00"), "W1"),
    ]
    assert flows[0].source_evidence == "ledgers.csv:refid:D1"


def test_saga_links_are_scoped_to_the_target_withdrawal() -> None:
    db = create_sample_graph()
    sources = SagaSources(
        transactions=[
            _buy("2023-01-02", refid="T1"),
            _withdrawal("2023-01-03", refid="W1"),
            _buy("2023-01-02", btc="2", eur="40000", refid="T2"),
            _withdrawal("2023-01-03", btc="2", refid="W2"),
        ],
        fiat_flows=[_deposit("2022-12-30"), _deposit("2022-12-30", eur="40000", refid="D2")],
        bank_rows=[_bank("2022-12-30", "-20000"), _bank("2022-12-30", "-40000", ref="D2", row=3)],
    )
    doc = build_saga_document(db, "D", 0, lambda _day: Decimal("20000"), sources=sources)
    refs = " ".join(link.from_ref + " " + link.to_ref for link in doc.links)
    assert "W1" in refs and "T1" in refs and "D1" in refs
    assert "W2" not in refs and "T2" not in refs and "D2" not in refs


def test_build_saga_links_marks_unmatched_exchange_source() -> None:
    db = create_sample_graph()
    nodes, _ = build_saga("D", 0, db, lambda _day: Decimal("20000"))
    sources = SagaSources(transactions=[_buy("2023-01-02")], fiat_flows=[], bank_rows=[])
    links, notes = build_saga_links("D", 0, nodes, sources)
    assert links == []
    assert any("source A:0" in note for note in notes)


def test_schema_upsert_does_not_raise_on_same_edge() -> None:
    db: sqlite3.Connection = create_sample_graph()
    link = ProvenanceLink("D", 0, "chain", "a", "b", "exact", "e")
    upsert_provenance_link(db, link)
    upsert_provenance_link(db, link)
    assert len(list_provenance_links(db, "D", 0)) == 1


def test_provenance_page_renders_all_parent_saga_and_links(tmp_path) -> None:
    from utxoproof.reports import write_provenance_page

    db = create_sample_graph()
    target = write_provenance_page(
        db,
        "D",
        0,
        lambda _day: Decimal("20000"),
        Decimal("40000"),
        datetime.date(2024, 6, 1),
        tmp_path,
        saga_sources=_full_sources(),
    )
    html = target.read_text(encoding="utf-8")
    assert "Saga coverage" in html
    assert "50%" in html
    assert "Provenance saga (all parents, oldest first)" in html
    assert "Cross-domain evidence" in html
    assert "bank:bank.csv:2" in html
    assert "allocated" in html
    assert "Fan-out siblings" in html
    assert "Candidate links (never asserted)" in html


def test_cli_provenance_persists_csv_cross_domain_links(tmp_path) -> None:
    from utxoproof.cli import main

    db_path = tmp_path / "utxoproof.db"
    source = create_sample_graph()
    dest = sqlite3.connect(db_path)
    source.backup(dest)
    dest.close()

    ledgers = tmp_path / "ledgers.csv"
    ledgers.write_text(
        "txid,refid,time,type,subtype,aclass,asset,amount,fee,balance\n"
        "L1,D1,2022-12-30 09:00:00,deposit,,currency,ZEUR,20000.00,0,20000\n"
        "L2,T1,2023-01-02 10:00:00,trade,,currency,XXBT,1.00000000,0,1\n"
        "L3,T1,2023-01-02 10:00:00,trade,,currency,ZEUR,-20000.00,0,0\n"
        "L4,W1,2023-01-03 11:00:00,withdrawal,,currency,XXBT,-1.00000000,0,0\n",
        encoding="utf-8",
    )
    bank = tmp_path / "bank.csv"
    bank.write_text(
        "Datum;Omschrijving;Tegenpartijrekening;Bedrag;Mededeling\n"
        "30/12/2022;Kraken storting;BE00;-20000,00;D1\n",
        encoding="utf-8",
    )
    rc = main(
        [
            "provenance",
            "D:0",
            "--db",
            str(db_path),
            "--price",
            "20000",
            "--as-of",
            "2024-06-01",
            "--ledgers",
            str(ledgers),
            "--bank-csv",
            str(bank),
            "--bank-type",
            "kbc-bankaccount",
            "--out",
            str(tmp_path / "out"),
        ]
    )
    assert rc == 0
    html = (tmp_path / "out" / "provenance_D_0.html").read_text(encoding="utf-8")
    assert "Saga coverage" in html
    assert "bank:bank.csv:2" in html
    assert "Cross-domain evidence" in html
    persisted = sqlite3.connect(db_path)
    try:
        count = persisted.execute(
            "SELECT count(*) FROM provenance_links WHERE utxo_txid='D' AND utxo_vout=0"
        ).fetchone()[0]
    finally:
        persisted.close()
    assert count == 4
