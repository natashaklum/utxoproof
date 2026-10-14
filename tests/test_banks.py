"""Bank parser + fiat-leg matching tests (Sprint 8)."""

import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from utxoproof.banks import (
    BANK_PROFILES,
    BANK_TYPES,
    BankRow,
    parse_amount,
    parse_bank_csv,
    resolve_bank,
)
from utxoproof.kraken_csv import parse_kraken_ledgers
from utxoproof.matching import match_fiat_legs

FIXTURES = Path(__file__).parent / "fixtures"


def test_all_bank_profiles_parse() -> None:
    expected = {
        "ing": (Decimal("20000"), "BE68539007547034"),
        "kbc": (Decimal("11000"), "BE12345678901234"),
        "bnp": (Decimal("2800"), "BE99999999999999"),
        "belfius": (Decimal("-85.40"), None),
        "argenta": (Decimal("-120.50"), None),
    }
    assert set(BANK_PROFILES) == {
        "ing",
        "kbc-bankaccount",
        "kbc-creditcard",
        "bnp",
        "belfius",
        "argenta",
    }
    assert "kbc" in BANK_TYPES  # alias kept working
    assert resolve_bank("kbc") == "kbc-bankaccount"
    assert resolve_bank("kbc-creditcard") == "kbc-creditcard"
    for bank, (amount, iban) in expected.items():
        rows = parse_bank_csv(FIXTURES / f"{bank}_2023.csv", bank)
        assert len(rows) >= 1
        assert rows[0].amount_eur == amount
        assert rows[0].counterparty_iban == iban
        assert rows[0].source_file.endswith(f"{bank}_2023.csv")


def test_parse_amount_formats() -> None:
    assert parse_amount("1.234,56", ",") == Decimal("1234.56")
    assert parse_amount("1234.56", ".") == Decimal("1234.56")
    assert parse_amount("", ",") == Decimal("0")


def test_unknown_bank_raises() -> None:
    with pytest.raises(ValueError, match="Unknown bank"):
        parse_bank_csv(FIXTURES / "ing_2023.csv", "revolut")


def test_kbc_creditcard_split_amounts_merchant_and_pii() -> None:
    rows = parse_bank_csv(FIXTURES / "kbc_creditcard_2023.csv", "kbc-creditcard")
    assert len(rows) == 3
    buy, refund, fx = rows
    # Dates use settlement (verrekening), amounts keep full precision.
    assert (buy.date.isoformat(), buy.amount_eur) == ("2023-06-16", Decimal("-42.50"))
    assert buy.description == "FAKE*Shop — example.com, België"
    assert buy.counterparty_iban is None
    assert buy.reference == "Example purchase"
    # Credit refund: positive, fee surfaced in the description.
    assert refund.amount_eur == Decimal("92")
    assert "+ €1.50 fee" in refund.description
    # Foreign row without a converted figure falls back to bedrag x koers.
    assert fx.amount_eur == Decimal("-45.000000000")
    # Card numbers and holder names are never ingested.
    blob = " ".join(
        f"{r.description} {r.reference or ''} {r.counterparty_iban or ''}" for r in rows
    )
    assert "999999XXXXXX0000" not in blob and "FAKE HOLDER" not in blob


def test_kbc_alias_parses_like_bankaccount() -> None:
    assert parse_bank_csv(FIXTURES / "kbc_2023.csv", "kbc") == parse_bank_csv(
        FIXTURES / "kbc_2023.csv", "kbc-bankaccount"
    )


def test_creditcard_crlf_and_foreign_without_rate(tmp_path: Path) -> None:
    raw = (
        "kredietkaart;kaarthouder;uitgavenstaat;datum verrichting;Datum verrekening;"
        "bedrag;credit;debet;munt;koers;bedrag in EUR;Kosten op verrichting;"
        "Handelaar;locatie;land;toelichting\r\n"
        "1;H;juni 2023;15/06/2023;16/06/2023;-42,50;;-42,50;EUR;1,00;-42,50;0,00;S;e;B;t\r\n"
    )
    crlf = tmp_path / "card.csv"
    crlf.write_bytes(raw.encode("utf-8"))
    (rows := parse_bank_csv(crlf, "kbc-creditcard"))
    assert len(rows) == 1 and rows[0].amount_eur == Decimal("-42.50")

    no_rate = tmp_path / "norf.csv"
    no_rate.write_text(
        "kredietkaart;kaarthouder;uitgavenstaat;datum verrichting;Datum verrekening;"
        "bedrag;credit;debet;munt;koers;bedrag in EUR;Kosten op verrichting;"
        "Handelaar;locatie;land;toelichting\n"
        "1;H;j 2023;10/07/2023;11/07/2023;50,00;;;USD;;;0,00;S;e;B;t\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="without rate"):
        parse_bank_csv(no_rate, "kbc-creditcard")


def test_fiat_leg_matching() -> None:
    txs = parse_kraken_ledgers(FIXTURES / "kraken_ledgers_2023.csv")
    bank_rows = parse_bank_csv(FIXTURES / "ing_matching.csv", "ing")
    matches, unmatched = match_fiat_legs(txs, bank_rows)
    assert len(matches) == 3
    assert matches[0].tx.refid == "R1"  # same-day exact amount, reference hit
    assert matches[0].tier == "strong"
    assert any("direction outflow<->BUY" in r for r in matches[0].reasons)
    assert matches[1].tx.refid == "R2"  # 1 day off, exact amount, reference hit
    assert matches[1].tier == "strong"
    assert matches[2].tx.refid == "R4"  # direction+amount+date, no reference
    assert matches[2].tier == "standard"
    assert len(unmatched) == 1
    assert unmatched[0].reference == "NOMATCH"


def test_fiat_leg_direction_mismatch_never_matches() -> None:
    from utxoproof.exchange import ExchangeTx

    txs = [
        ExchangeTx(
            date=datetime.date(2023, 1, 10),
            kind="BUY",
            btc=Decimal("1"),
            eur_per_btc=Decimal("20000"),
            fee_eur=Decimal("0"),
            refid="X",
        )
    ]
    bank_rows = parse_bank_csv(FIXTURES / "ing_matching.csv", "ing")
    # Flip the R1 outflow into an inflow: same date+amount, wrong direction.
    bank_rows[0] = BankRow(
        date=bank_rows[0].date,
        description=bank_rows[0].description,
        amount_eur=Decimal("20000"),
        counterparty_iban=bank_rows[0].counterparty_iban,
        reference=bank_rows[0].reference,
    )
    matches, unmatched = match_fiat_legs(txs, bank_rows)
    assert matches == []
    assert len(unmatched) == len(bank_rows)
