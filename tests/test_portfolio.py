"""Portfolio dashboard tests (synthetic entities, hand-computed)."""

from decimal import Decimal
from pathlib import Path

from utxoproof.portfolio import (
    Entity,
    FiatHolding,
    Portfolio,
    UtxoHolding,
    svg_bars,
    svg_sparkline,
)

PRICE = Decimal("40000")


def _portfolio() -> Portfolio:
    return Portfolio(
        entities=[
            Entity("cold", "wallet", "Cold storage", ""),
            Entity("bank", "bank", "ING savings", ""),
        ],
        utxos=[
            UtxoHolding("cold", "A", 0, Decimal("2"), Decimal("80000"), "kyc", Decimal("40000")),
            UtxoHolding(
                "cold", "C", 1, Decimal("0.5"), Decimal("20000"), "mixed", Decimal("12500")
            ),
        ],
        fiat=[FiatHolding("bank", Decimal("12500"), "buffer")],
        current_price_eur=PRICE,
    )


def test_totals_and_allocation() -> None:
    portfolio = _portfolio()
    assert portfolio.btc_total == Decimal("2.5")
    assert portfolio.btc_value_eur == Decimal("100000")
    assert portfolio.fiat_total_eur == Decimal("12500")
    assert portfolio.net_worth_eur == Decimal("112500")
    assert portfolio.cost_basis_eur == Decimal("52500")
    assert portfolio.unrealized_eur == Decimal("47500")
    allocation = portfolio.allocation()
    assert [row[0] for row in allocation] == ["cold", "bank"]
    assert allocation[0][2] == Decimal("100000")
    assert sum(row[3] for row in allocation) == Decimal("100")


def test_kyc_split_canonical_order_and_stable_colors() -> None:
    from utxoproof.portfolio import donut_legend

    # Deliberate tie in demo data must not reshuffle status colors.
    assert _portfolio().kyc_split() == [
        ("kyc", Decimal("80000")),
        ("mixed", Decimal("20000")),
    ]
    legend = {label: color for color, label, _ in donut_legend(_portfolio().kyc_split())}
    assert legend == {"kyc": "#2f6fed", "mixed": "#e8a13d"}


def test_charts_contain_labels_and_links() -> None:
    bars = svg_bars(
        [("Cold", Decimal("100000"), "../entities/cold.html"), ("Bank", Decimal("12500"), "")]
    )
    assert "<svg" in bars and "Cold" in bars and "../entities/cold.html" in bars
    assert "100,000.00" in bars
    from utxoproof.portfolio import donut_ring

    ring = donut_ring([("kyc", Decimal("80000")), ("mixed", Decimal("20000"))])
    assert "conic-gradient" in ring
    assert "#2f6fed 0.0% 80.0%" in ring  # kyc first, stable color
    assert "#e8a13d 80.0% 100.0%" in ring
    assert "100,000" in ring and "80%" in ring


def test_sparkline_points_are_valid_polyline() -> None:
    import re

    from utxoproof.portfolio import svg_sparkline

    svg = svg_sparkline([("2025-01-01", Decimal("50000")), ("2025-06-01", Decimal("60000"))])
    points = re.search(r'points="([^"]*)"', svg).group(1)
    assert points and not any(c.isalpha() for c in points)
    assert "<polyline" in svg


def test_overview_and_entity_pages(tmp_path: Path) -> None:
    from utxoproof.reports import write_entity_pages, write_overview_page

    portfolio = _portfolio()

    spark = svg_sparkline([("2024-01-01", Decimal("40000")), ("2024-06-01", Decimal("60000"))])
    target = write_overview_page(portfolio, "2024-06-01", "test price", tmp_path, spark)
    html = target.read_text(encoding="utf-8")
    assert "112,500.00" in html  # net worth
    assert "52,500.00" in html  # cost basis
    assert "47,500.00" in html  # unrealized
    assert "<polyline" in html
    assert "High 60,000" in html and "Low 40,000" in html
    assert "../entities/cold.html" in html
    assert "Cold storage" in html
    assert "<svg" in html and "&lt;svg" not in html  # charts not escaped
    assert 'class="legend"' in html and "#2f6fed" in html  # HTML legend
    assert "Cold storage (89%)" in html  # share on bars

    pages = write_entity_pages(
        portfolio, "../provenance", tmp_path / "entities", {"A:0": "hold_recommended"}
    )
    assert len(pages) == 2
    cold = (tmp_path / "entities" / "cold.html").read_text(encoding="utf-8")
    assert "../provenance/provenance_A_0.html" in cold
    assert "mixed" in cold
    assert "hold_recommended" in cold
    bank = (tmp_path / "entities" / "bank.html").read_text(encoding="utf-8")
    assert "12,500.00" in bank
    assert "provenance_" not in bank  # no UTXOs, no provenance links


def _sample_db_with_spendable() -> object:
    from utxoproof.kyc import create_sample_graph

    return create_sample_graph()


def test_load_entities_example(tmp_path: Path) -> None:
    import shutil

    from utxoproof.portfolio import load_entities

    src = Path(__file__).resolve().parent.parent / "examples" / "entities.example.toml"
    dst = tmp_path / "entities.toml"
    shutil.copy(src, dst)
    entities, fiat = load_entities(dst)
    assert [e.id for e in entities] == ["ledger-savings", "ing-savings", "kraken-eur"]
    assert entities[0].wallet == "utxoproof_watchonly"
    assert sum((f.amount_eur for f in fiat), Decimal("0")) == Decimal("15700")


def test_load_entities_rejects_unknown_fiat_entity(tmp_path: Path) -> None:
    import pytest

    from utxoproof.portfolio import load_entities

    cfg = tmp_path / "bad.toml"
    cfg.write_text(
        '[[entities]]\nid = "only"\n[[fiat]]\nentity_id = "ghost"\namount_eur = "5"\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="unknown entity"):
        load_entities(cfg)


def test_portfolio_from_db_assigns_utxos_to_wallet_entity() -> None:
    import datetime

    from utxoproof.portfolio import Entity, portfolio_from_db

    db = _sample_db_with_spendable()
    entities = [Entity("cold", "wallet", "Cold", "", wallet="w1")]
    portfolio, advisories = portfolio_from_db(
        db,
        entities,
        [],
        "w1",
        lambda _day: Decimal("20000"),
        Decimal("40000"),
        datetime.date(2024, 6, 1),
    )
    assert advisories, "sample graph must have unspent outputs"
    assert {u.entity_id for u in portfolio.utxos} == {"cold"}
    assert portfolio.btc_total == sum((a.amount_btc for a in advisories), Decimal("0"))


def test_portfolio_cli_writes_overview_and_entities(tmp_path: Path) -> None:
    import shutil
    import sqlite3

    from utxoproof.cli import main
    from utxoproof.kyc import create_sample_graph

    db_path = tmp_path / "t.db"
    dest = sqlite3.connect(str(db_path))
    create_sample_graph().backup(dest)
    dest.close()
    shutil.copy(
        Path(__file__).resolve().parent.parent / "examples" / "entities.example.toml",
        tmp_path / "entities.toml",
    )
    out = tmp_path / "site"
    assert (
        main(
            [
                "portfolio",
                "--db",
                str(db_path),
                "--entities",
                str(tmp_path / "entities.toml"),
                "--price",
                "40000",
                "--as-of",
                "2024-06-01",
                "--out",
                str(out),
            ]
        )
        == 0
    )
    assert (out / "overview" / "overview.html").is_file()
    assert (out / "entities" / "ledger-savings.html").is_file()


def test_address_column_flows_to_entity_pages(tmp_path: Path) -> None:
    import datetime
    from decimal import Decimal

    from utxoproof.advisory import analyze_wallet
    from utxoproof.kyc import create_sample_graph
    from utxoproof.portfolio import Entity, portfolio_from_db
    from utxoproof.reports import write_entity_pages

    db = create_sample_graph()
    addr = "bcrt1qtestaddress000000000000000000000000"
    db.execute("UPDATE tx_outputs SET address = ? WHERE txid = 'D'", (addr,))
    db.commit()
    advisories = analyze_wallet(
        db, lambda _d: Decimal("20000"), Decimal("40000"), datetime.date(2024, 6, 1)
    )
    assert any(a.address == addr for a in advisories)
    portfolio, _ = portfolio_from_db(
        db,
        [Entity("cold", "wallet", "Cold", "", wallet="w")],
        [],
        "w",
        lambda _d: Decimal("20000"),
        Decimal("40000"),
        datetime.date(2024, 6, 1),
    )
    assert any(u.address == addr for u in portfolio.utxos)
    targets = write_entity_pages(portfolio, "../provenance", tmp_path)
    html = targets[0].read_text(encoding="utf-8")
    assert "<th>Address</th>" in html
    assert addr in html
    assert "—" in html  # outputs without a recorded address fall back gracefully
