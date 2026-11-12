"""Bisq receipts, manual exchange events and linked review pages."""

from __future__ import annotations

import datetime
import shutil
from decimal import Decimal
from pathlib import Path

import pytest

from utxoproof.accounts import AccountError, load_account, load_registry
from utxoproof.banks import parse_bank_csv
from utxoproof.bisq_csv import parse_bisq_trade_history
from utxoproof.bisq_receipts import match_receipts, parse_bisq_receipt
from utxoproof.manual_exchange import parse_manual_events
from utxoproof.matching import match_fiat_legs

EXAMPLE = Path(__file__).parent.parent / "examples" / "input_v8"
BISQ = EXAMPLE / "accounts" / "bisq"
OLD = EXAMPLE / "accounts" / "old-exchange"


def test_parse_receipt_filename() -> None:
    path = next((BISQ / "receipts").glob("20230227_*.pdf"))
    receipt = parse_bisq_receipt(path)
    assert receipt.date == datetime.date(2023, 2, 27)
    assert receipt.currency == "EUR"
    assert receipt.btc == Decimal("0.01234")
    assert receipt.price_eur == Decimal("25000.00")
    assert receipt.market == "EURBTC"
    assert receipt.iban == "ES7200495NNNNNNNNN"
    assert receipt.bic == "BSCHESXXXXX"
    assert receipt.name == "ALICE EXAMPLE"
    assert receipt.reference == "0001"
    assert receipt.own_iban == "BE68539007547034"
    assert receipt.eur_amount == Decimal("308.5000")


def test_parse_receipt_rejects_non_bisq_name(tmp_path: Path) -> None:
    path = tmp_path / "receipt.pdf"
    path.write_bytes(b"%PDF-1.4\n")
    with pytest.raises(ValueError, match="Bisq SEPA receipt"):
        parse_bisq_receipt(path)


def test_receipts_match_trades_and_bank_rows() -> None:
    trades, warnings = parse_bisq_trade_history(
        BISQ / "tradeHistory.csv", btc_eur=lambda _day: Decimal("30000")
    )
    assert warnings and "Canceled" in warnings[0]
    rows = parse_bank_csv(
        EXAMPLE / "accounts" / "kbc-checking" / "statements" / "2023" / "bisq.csv",
        "kbc-bankaccount",
    )
    bank_matches, unmatched = match_fiat_legs(trades, rows)
    assert len(bank_matches) == 2
    assert unmatched == []

    receipts = [parse_bisq_receipt(path) for path in sorted((BISQ / "receipts").glob("*.pdf"))]
    matches, receipt_warnings = match_receipts(receipts, trades, bank_matches)
    assert receipt_warnings == []
    assert [match.tier for match in matches] == ["exact", "exact"]
    assert [match.trade.refid for match in matches] == [
        "BISQ-2023-0001",
        "BISQ-2023-0002",
    ]
    assert matches[0].bank is not None
    assert matches[0].bank.amount_eur == Decimal("-308.50")


def test_unmatched_receipt_is_reported(tmp_path: Path) -> None:
    trades, _warnings = parse_bisq_trade_history(
        BISQ / "tradeHistory.csv", btc_eur=lambda _day: Decimal("30000")
    )
    source = next((BISQ / "receipts").glob("20230227_*.pdf"))
    other = tmp_path / source.name.replace("20230227", "20230309")
    shutil.copy2(source, other)
    receipt = parse_bisq_receipt(other)
    matches, warnings = match_receipts([receipt], trades, [])
    assert matches[0].tier == "unmatched"
    assert matches[0].trade is None
    assert any("no matching Bisq trade" in warning for warning in warnings)


def test_ambiguous_receipt_is_not_guessed() -> None:
    receipts = [parse_bisq_receipt(next((BISQ / "receipts").glob("20230227_*.pdf")))]
    trades, _warnings = parse_bisq_trade_history(
        BISQ / "tradeHistory.csv", btc_eur=lambda _day: Decimal("30000")
    )
    duplicate = next(tx for tx in trades if tx.refid == "BISQ-2023-0001")
    duplicate.refid = "BISQ-2023-0001-COPY"
    matches, warnings = match_receipts(receipts, [*trades, duplicate], [])
    assert matches[0].tier == "ambiguous"
    assert matches[0].trade is None
    assert any("candidate trades" in warning for warning in warnings)


def test_receipt_does_not_cross_accounts() -> None:
    receipt = parse_bisq_receipt(next((BISQ / "receipts").glob("20230227_*.pdf")))
    receipt.account_id = "bisq"
    trades, _warnings = parse_bisq_trade_history(
        BISQ / "tradeHistory.csv", btc_eur=lambda _day: Decimal("30000")
    )
    for trade in trades:
        trade.account_id = "other-exchange"
    matches, _warnings = match_receipts([receipt], trades, [])
    assert matches[0].tier == "unmatched"
    assert matches[0].trade is None


def test_manual_events_parse_with_evidence() -> None:
    txs = parse_manual_events(OLD / "events.csv", OLD)
    assert [(tx.kind, tx.date) for tx in txs] == [
        ("BUY", datetime.date(2017, 11, 20)),
        ("WITHDRAWAL", datetime.date(2018, 1, 15)),
    ]
    buy = txs[0]
    assert buy.exchange == "manual"
    assert buy.kyc_status == "kyc"
    assert buy.evidence == ("evidence/2017-11-20_purchase_confirmation.eml",)
    assert buy.fiat_amount == Decimal("1500.0000")
    assert txs[1].address.startswith("bc1q")
    assert txs[1].txid


def test_manual_events_require_existing_evidence(tmp_path: Path) -> None:
    events = tmp_path / "events.csv"
    events.write_text(
        "date,kind,btc,eur_per_btc,evidence\n2018-01-01,BUY,0.1,5000,missing.eml\n",
        encoding="utf-8",
    )
    (tmp_path / "account.toml").write_text(
        'kind = "exchange"\nlabel = "Manual"\nexchange = "manual"\nevents = "events.csv"\n',
        encoding="utf-8",
    )
    assert load_account(tmp_path).exchange == "manual"
    with pytest.raises(ValueError, match="evidence file not found"):
        parse_manual_events(events, tmp_path)


def test_manual_events_reject_unknown_kind(tmp_path: Path) -> None:
    events = tmp_path / "events.csv"
    events.write_text("date,kind,btc\n2018-01-01,TRANSFER,0.1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="kind must be one of"):
        parse_manual_events(events, tmp_path)


def test_example_tree_loads_exchange_adapters() -> None:
    registry = load_registry(EXAMPLE)
    assert registry.ok, registry.errors
    bisq = registry.get("bisq")
    assert bisq.exchange == "bisq"
    assert len(bisq.trades) == 1
    assert len(bisq.transactions) == 1
    assert len(bisq.receipts) == 2
    manual = registry.get("old-exchange")
    assert manual.exchange == "manual"
    assert len(manual.events) == 1


def test_bisq_needs_at_least_one_source(tmp_path: Path) -> None:
    (tmp_path / "account.toml").write_text(
        'kind = "exchange"\nlabel = "Bisq"\nexchange = "bisq"\n', encoding="utf-8"
    )
    with pytest.raises(AccountError, match=r"tradeHistory\.csv"):
        load_account(tmp_path)
