"""Sprint 0 deliverable: compute --input manual.csv --year 2023.

Fixture is 12 synthetic rows (11 in 2023 + 1 in 2024 to prove year filtering).
Expected gain cross-checked by an independent step-by-step moving-average
recomputation: 4337.670995670995670995670995 EUR.
"""

from decimal import Decimal
from pathlib import Path

from utxoproof.belgian_tax import apply_belgian_tax
from utxoproof.cli import compute_year

FIXTURE = Path(__file__).parent / "fixtures" / "manual_2023.csv"
EXPECTED_GAIN_2023 = Decimal("4337.670995670995670995670995")


def test_compute_2023_gain() -> None:
    result = compute_year(FIXTURE, 2023)
    assert result["gain_loss_eur"] == EXPECTED_GAIN_2023


def test_compute_applies_belgian_tax() -> None:
    gain = compute_year(FIXTURE, 2023)["gain_loss_eur"]
    tax = apply_belgian_tax(gain)
    assert tax["tax_eur"] == gain * Decimal("0.33")
    assert tax["total_eur"] == tax["tax_eur"] + tax["communal_surcharge_eur"]


def test_compute_2024_only_counts_2024_sale() -> None:
    result = compute_year(FIXTURE, 2024)
    # Only the 2024-01-15 SELL counts; basis is the moving average at that point.
    assert result["gain_loss_eur"] == Decimal("1827.932900432900432900432901")


def test_margin_sell_on_empty_inventory_names_cause() -> None:
    """Real-data vector: margin disposal (likely short sale) with no inventory."""
    import pytest

    from utxoproof.cli import compute_year

    fixture = Path(__file__).parent / "fixtures" / "margin_short.csv"
    with pytest.raises(ValueError, match=r"MARGIN.*empty inventory.*short sale"):
        compute_year(fixture, 2021, strict=True)


def test_csv_years_lists_data_years_only() -> None:
    from utxoproof.cli import csv_years

    assert csv_years(FIXTURE) == [2022, 2023, 2024]


def test_compute_all_years_prints_per_year_table(capsys) -> None:
    from utxoproof.cli import main

    assert main(["compute", "--input", str(FIXTURE), "--all-years"]) == 0
    out = capsys.readouterr().out
    assert "year: 2022 gain_loss_eur: 0.00" in out
    assert "year: 2023 gain_loss_eur: 4337.67" in out
    assert "total_gain_loss_eur:" in out


def test_compute_year_flags_are_mutually_exclusive() -> None:
    import pytest

    from utxoproof.cli import main

    with pytest.raises(SystemExit):
        main(["compute", "--input", str(FIXTURE), "--year", "2023", "--all-years"])
    with pytest.raises(SystemExit):
        main(["compute", "--input", str(FIXTURE)])
