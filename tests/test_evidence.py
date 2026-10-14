"""Evidence ZIP + all-time summary tests (Sprint 9)."""

import json
import sqlite3
import zipfile
from decimal import Decimal
from pathlib import Path

from utxoproof.cli import compute_alltime, main
from utxoproof.db import open_memory_db
from utxoproof.evidence import (
    produce_evidence_zip,
    record_source,
    sha256_file,
)

FIXTURE = Path(__file__).parent / "fixtures" / "manual_2023.csv"
KRAKEN = Path(__file__).parent / "fixtures" / "kraken_ledgers_2023.csv"


def test_alltime_numbers() -> None:
    result = compute_alltime(FIXTURE)
    by_year = {row["year"]: row for row in result["per_year"]}
    assert set(by_year) == {2022, 2023, 2024}
    assert by_year[2022] == {"year": 2022, "gain_eur": Decimal("0"), "disposals": 0}
    assert by_year[2023]["gain_eur"] == Decimal("4337.670995670995670995670995")
    assert by_year[2023]["disposals"] == 5
    assert by_year[2024]["gain_eur"] == Decimal("1827.932900432900432900432901")
    assert result["inventory"] == {
        "btc": Decimal("0.90000000"),
        "cost_eur": Decimal("19548.60389610389610389610389"),
    }


def test_evidence_zip_contents(tmp_path: Path) -> None:
    db = open_memory_db()
    assert record_source(db, KRAKEN, "source") is True
    assert record_source(db, KRAKEN, "source") is False  # content dedupe
    assert record_source(db, FIXTURE, "source") is True

    manifest_db_path = tmp_path / "evidence.db"
    dest = sqlite3.connect(str(manifest_db_path))
    db.backup(dest)
    dest.close()
    report_html = tmp_path / "report.html"
    report_html.write_text("<html></html>", encoding="utf-8")

    zip_path = produce_evidence_zip(
        2023,
        manifest_db_path,
        report_html,
        [KRAKEN],
        [FIXTURE],
        tmp_path,
        timestamp="20240101T000000Z",
    )
    assert zip_path.name == "utxoproof_evidence_2023_20240101T000000Z.zip"
    with zipfile.ZipFile(zip_path) as zf:
        names = set(zf.namelist())
    assert names == {
        "sources/kraken_ledgers_2023.csv",
        "intermediate/manual_2023.csv",
        "report.html",
        "evidence.db",
        "manifest.json",
    }
    with zipfile.ZipFile(zip_path) as zf:
        manifest = json.loads(zf.read("manifest.json"))
    assert manifest["tax_year"] == 2023
    assert manifest["utxoproof_version"]
    roles = {f["path"]: f["role"] for f in manifest["files"]}
    assert roles["sources/kraken_ledgers_2023.csv"] == "source"
    assert roles["intermediate/manual_2023.csv"] == "intermediate"
    assert roles["report.html"] == "report"
    shas = {f["path"]: f["sha256"] for f in manifest["files"]}
    assert shas["sources/kraken_ledgers_2023.csv"] == sha256_file(KRAKEN)
    assert all(len(f["sha256"]) == 64 for f in manifest["files"])


def test_report_cli_zip_and_alltime(tmp_path: Path) -> None:
    out = tmp_path / "r2023"
    assert (
        main(
            [
                "report",
                "--input",
                str(FIXTURE),
                "--year",
                "2023",
                "--source",
                str(KRAKEN),
                "--out",
                str(out),
            ]
        )
        == 0
    )
    zips = list(out.glob("utxoproof_evidence_2023_*.zip"))
    assert len(zips) == 1
    assert (out / "evidence-manifest.db").exists()

    out_all = tmp_path / "alltime"
    assert main(["report", "--input", str(FIXTURE), "--alltime", "--out", str(out_all)]) == 0
    html = (out_all / "summary.html").read_text(encoding="utf-8")
    assert "6165.60" in html and "0.90000000" in html

    out_nozip = tmp_path / "r2024"
    assert (
        main(
            [
                "report",
                "--input",
                str(FIXTURE),
                "--year",
                "2024",
                "--no-zip",
                "--out",
                str(out_nozip),
            ]
        )
        == 0
    )
    assert list(out_nozip.glob("*.zip")) == []


def test_report_all_years_writes_per_year_dirs(tmp_path: Path) -> None:
    out = tmp_path / "years"
    assert (
        main(
            [
                "report",
                "--input",
                str(FIXTURE),
                "--all-years",
                "--source",
                str(KRAKEN),
                "--out",
                str(out),
            ]
        )
        == 0
    )
    for year in ("2022", "2023", "2024"):
        assert (out / year / "report.html").is_file()
        assert len(list((out / year).glob(f"utxoproof_evidence_{year}_*.zip"))) == 1


def test_report_year_flags_are_mutually_exclusive() -> None:
    import pytest

    from utxoproof.cli import main

    with pytest.raises(SystemExit):
        main(["report", "--input", str(FIXTURE), "--year", "2023", "--alltime", "--out", "x"])
    with pytest.raises(SystemExit):
        main(["report", "--input", str(FIXTURE), "--out", "x"])
