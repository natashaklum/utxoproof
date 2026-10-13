"""Price-history fetch/stitch tests (offline; synthetic inputs + roundtrip)."""

import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, "scripts")

from fetch_price_history import assert_gapless, stitch_btc_eur


def test_stitch_kraken_wins_overlaps_and_zeros_skipped() -> None:
    eur = stitch_btc_eur(
        {"2024-01-01": Decimal("0"), "2024-01-02": Decimal("40000")},
        {"2024-01-02": Decimal("41000")},
        {"2024-01-01": Decimal("1.1"), "2024-01-02": Decimal("1.1")},
    )
    # Zero USD prints carry no signal, so 2024-01-01 is absent entirely.
    assert set(eur) == {"2024-01-02"}
    # Kraken native EUR wins the overlap over the converted print.
    assert eur["2024-01-02"] == ("41000", "kraken")


def test_stitch_converts_usd_via_ecb() -> None:
    eur = stitch_btc_eur(
        {"2024-01-03": Decimal("44000")},
        {},
        {"2024-01-03": Decimal("1.1")},
    )
    assert set(eur) == {"2024-01-03"}
    assert eur["2024-01-03"][1] == "blockchain.info-x-ecb"
    assert Decimal(eur["2024-01-03"][0]) == Decimal("40000")


def test_assert_gapless_raises_naming_gaps() -> None:
    import pytest

    with pytest.raises(ValueError, match=r"1 gap day.*2024-01-02"):
        assert_gapless(["2024-01-01", "2024-01-03", "2024-01-04"])
    assert_gapless(["2024-01-01", "2024-01-02", "2024-01-03"])  # no raise


def test_offline_restitch_matches_shipped_file() -> None:
    """Raw files on disk re-stitch to exactly the shipped BTC/EUR file."""
    import csv

    data = Path("utxoproof/data")
    early_usd: dict[str, Decimal] = {}
    with open(data / "btc_usd_daily.csv", newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            early_usd[row["date"]] = Decimal(row["close_usd"])
    ecb: dict[str, Decimal] = {}
    with open(data / "usd_eur_daily.csv", newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            ecb[row["date"]] = Decimal(row["usd_per_eur"])
    kraken: dict[str, Decimal] = {}
    shipped: dict[str, tuple[str, str]] = {}
    with open(data / "btc_eur_daily.csv", newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            shipped[row["date"]] = (row["close_eur"], row["source"])
            if row["source"] == "kraken":
                kraken[row["date"]] = Decimal(row["close_eur"])
    assert stitch_btc_eur(early_usd, kraken, ecb) == shipped
