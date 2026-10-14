"""First HTML tax report (demo data)."""

from decimal import Decimal
from pathlib import Path

import pytest

from utxoproof.reports import build_report, render_report, write_report

FIXTURE = Path(__file__).parent / "fixtures" / "manual_2023.csv"
KRAKEN = Path(__file__).parent / "fixtures" / "kraken_ledgers_2023.csv"


def test_report_totals_match_compute() -> None:
    report = build_report(FIXTURE, 2023)
    assert report.gain_eur == Decimal("4337.670995670995670995670995")
    assert report.tax_eur == report.gain_eur * Decimal("0.33")
    assert report.total_eur == report.tax_eur + report.communal_eur
    assert len(report.disposals) == 5
    assert sum((d.gain_eur for d in report.disposals), Decimal("0")) == report.gain_eur


def test_report_html_contains_figures(tmp_path: Path) -> None:
    target = write_report(FIXTURE, 2023, tmp_path)
    html = target.read_text(encoding="utf-8")
    assert "utxoproof tax report 2023" in html
    assert "4337.67" in html  # gain
    assert "1431.43" in html  # tax
    assert "goede huisvader" in html
    assert "Speculator score" in html
    assert "fiscaal adviseur" in html


def test_kraken_report_end_to_end(tmp_path: Path) -> None:
    import csv

    from utxoproof.kraken_csv import parse_kraken_ledgers, to_manual_csv_rows

    rows = to_manual_csv_rows(parse_kraken_ledgers(KRAKEN))
    csv_path = tmp_path / "kraken.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["date", "side", "kind", "trade_refs", "btc", "eur_per_btc", "fee_eur"],
            extrasaction="ignore",
        )
        writer.writeheader()
        writer.writerows(rows)
    report = build_report(csv_path, 2023, price_at=lambda _day: Decimal("30000"))
    assert report.gain_eur == Decimal("2905")
    html = render_report(report)
    assert "2905.00" in html and "958.65" in html


def test_descriptors_page_shows_vector_addresses(tmp_path: Path) -> None:
    from utxoproof.reports import write_descriptors_page

    target = write_descriptors_page(tmp_path)
    html = target.read_text(encoding="utf-8")
    assert "bc1qcr8te4kr609gcawutmrza0j4xv80jy8z306fyu" in html
    assert "bc1qnjg0jd8228aq7egyzacy8cys3knf9xvrerkf9g" in html
    assert "bc1q8c6fshw2dlwun7ekn9qwf37cu2rn755upcp6el" in html
    assert "73c5da0a" in html


def test_privacy_page_and_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    import sqlite3

    from utxoproof.cli import main
    from utxoproof.kyc import create_sample_graph
    from utxoproof.reports import write_privacy_page

    target = write_privacy_page(create_sample_graph(), tmp_path)
    html = target.read_text(encoding="utf-8")
    assert "mixed" in html and "mixing-origin" in html

    db_path = tmp_path / "demo.db"
    source = create_sample_graph()
    dest = sqlite3.connect(str(db_path))
    source.backup(dest)
    dest.close()
    assert main(["privacy", "--db", str(db_path), "--out", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "mixed=2" in out and "mixing_events: 2" in out


def test_demo_banner_only_when_requested(tmp_path: Path) -> None:
    from utxoproof.reports import write_status_page

    plain = write_status_page(
        Path(__file__).parent / "fixtures" / "manual_2023.csv",
        Decimal("40000"),
        "test",
        tmp_path / "plain",
    )
    assert '<div class="demo-banner">' not in plain.read_text(encoding="utf-8")
    flagged = write_status_page(
        Path(__file__).parent / "fixtures" / "manual_2023.csv",
        Decimal("40000"),
        "test",
        tmp_path / "demo",
        demo_notice="DEMO \u2014 dummy",
    )
    html = flagged.read_text(encoding="utf-8")
    assert '<div class="demo-banner">DEMO \u2014 dummy</div>' in html


def test_docs_page_builds(tmp_path: Path) -> None:
    import sys

    pytest.importorskip("markdown")
    sys.path.insert(0, "scripts")
    from build_demo import _write_docs_page

    out = tmp_path / "site"
    out.mkdir()
    _write_docs_page(out)
    html = (out / "docs" / "usage.html").read_text(encoding="utf-8")
    assert "utxoproof user guide" in html and "demo-banner" in html


def test_full_report_composes_all_sections_once(tmp_path: Path) -> None:
    import datetime
    from decimal import Decimal as _Decimal

    from utxoproof.kyc import create_sample_graph
    from utxoproof.portfolio import Entity, Portfolio, UtxoHolding
    from utxoproof.reports import write_full_report

    db = create_sample_graph()
    curve = {
        datetime.date(2023, 1, 1): _Decimal("20000"),
        datetime.date(2023, 2, 1): _Decimal("25000"),
        datetime.date(2023, 3, 1): _Decimal("30000"),
    }
    portfolio = Portfolio(
        entities=[Entity("cold", "wallet", "Cold", "")],
        utxos=[
            UtxoHolding(
                "cold", "D", 0, _Decimal("1.5"), _Decimal("60000"), "mixed", _Decimal("45000")
            )
        ],
        fiat=[],
        current_price_eur=_Decimal("40000"),
    )
    target = write_full_report(
        db=db,
        csv_path=Path(__file__).parent / "fixtures" / "manual_2023.csv",
        year=2023,
        out_dir=tmp_path,
        price_at=lambda day: curve[day],
        current_price_eur=_Decimal("40000"),
        price_note="test curve",
        as_of=datetime.date(2024, 6, 1),
        provenance_targets=[("D", 0)],
        portfolio=portfolio,
        demo_notice="DEMO",
    )
    html = target.read_text(encoding="utf-8")
    for anchor in (
        "#overview",
        "#tax",
        "#status",
        "#alltime",
        "#privacy",
        "#advisory",
        "#provenance-D-0",
        "#descriptors",
    ):
        assert html.count(f'href="{anchor}"') == 1, anchor
        assert html.count(f'id="{anchor[1:]}"') == 1, anchor
    # Section bodies appear exactly once (no duplicated markup).
    assert html.count("Annual summary") == 1
    assert html.count("Mixing events") == 1
    assert "utxoproof full report 2023" in html
    assert "What you owe for the year" in html  # intros travel along


def test_cli_full_report(tmp_path: Path) -> None:
    import sqlite3

    from utxoproof.cli import main
    from utxoproof.kyc import create_sample_graph

    prices = tmp_path / "prices.csv"
    prices.write_text(
        "date,close_eur\n2023-01-01,20000\n2023-01-02,20000\n2023-02-01,25000\n2023-03-01,30000\n",
        encoding="utf-8",
    )
    db_path = tmp_path / "full.db"
    dest = sqlite3.connect(str(db_path))
    create_sample_graph().backup(dest)
    dest.close()
    out = tmp_path / "full-out"
    assert (
        main(
            [
                "report",
                "--input",
                str(Path(__file__).parent / "fixtures" / "manual_2023.csv"),
                "--year",
                "2023",
                "--out",
                str(out),
                "--db",
                str(db_path),
                "--price",
                "40000",
                "--as-of",
                "2024-06-01",
                "--full",
                "--utxo",
                "D:0",
                "--no-zip",
                "--price-history",
                str(prices),
            ]
        )
        == 0
    )
    html = (out / "fullreport.html").read_text(encoding="utf-8")
    assert "utxoproof full report 2023" in html
    assert "Consolidation (2 inputs merged)" in html


def test_bank_page_renders_rows_matches_and_review(tmp_path: Path) -> None:
    from utxoproof.banks import parse_bank_csv
    from utxoproof.exchange import ExchangeTx
    from utxoproof.matching import FiatMatch
    from utxoproof.reports import write_bank_page

    rows = parse_bank_csv(Path(__file__).parent / "fixtures" / "kbc_2023.csv", "kbc")
    assert len(rows) == 1
    tx = ExchangeTx(
        date=rows[0].date,
        kind="BUY",
        btc=Decimal("0.5"),
        eur_per_btc=Decimal("22000"),
        fee_eur=Decimal("0"),
        refid="T1",
    )
    match = FiatMatch(tx, rows[0], ["date +0d", "direction outflow<->BUY"], "standard")
    target = write_bank_page("kbc", rows, [match], [], tmp_path)
    html = target.read_text(encoding="utf-8")
    assert "BINANCE storting" in html
    assert "11,000.00" in html
    assert "BUY 0.5 BTC" in html
    assert "direction outflow&lt;-&gt;BUY" in html
    assert "Nothing to review." in html

    target = write_bank_page("kbc", rows, [], rows, tmp_path)
    html = target.read_text(encoding="utf-8")
    assert "manual review" in html.lower() or "Manual review" in html


def test_site_nav_relativizes_marks_current_and_plans_missing(tmp_path: Path) -> None:
    from utxoproof.reports import latest_tax_year, site_nav

    site = tmp_path / "site"
    (site / "2023").mkdir(parents=True)
    (site / "2023" / "report.html").write_text("x", encoding="utf-8")
    (site / "bank").mkdir()
    page = site / "bank" / "bank.html"
    page.write_text("x", encoding="utf-8")
    assert latest_tax_year(site) == 2023
    items = site_nav(
        [("Tax 2023", "2023/report.html"), ("Bank review", "bank/bank.html")],
        site,
        page,
    )
    assert items == [
        {"title": "Tax 2023", "href": "../2023/report.html", "current": False},
        {"title": "Bank review", "href": "bank.html", "current": True},
    ]


def test_bank_command_matches_manual_with_reasons(tmp_path: Path) -> None:
    import csv

    from utxoproof.cli import main

    manual = tmp_path / "manual.csv"
    with open(manual, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["date", "side", "btc", "eur_per_btc", "fee_eur"])
        writer.writeheader()
        writer.writerow(
            {
                "date": "2023-02-15",
                "side": "SELL",
                "btc": "0.5",
                "eur_per_btc": "22000",
                "fee_eur": "0",
            }
        )
    out = tmp_path / "bank"
    assert (
        main(
            [
                "bank",
                "--file",
                str(Path(__file__).parent / "fixtures" / "kbc_2023.csv"),
                "--type",
                "kbc",
                "--manual",
                str(manual),
                "--out",
                str(out),
            ]
        )
        == 0
    )
    html = (out / "bank.html").read_text(encoding="utf-8")
    assert "match [standard]" in html or "standard" in html
    assert "direction inflow&lt;-&gt;SELL" in html
    assert "Nothing to review." in html
