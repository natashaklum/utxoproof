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
            UtxoHolding(
                "cold",
                "A",
                0,
                Decimal("2"),
                Decimal("80000"),
                "kyc",
                Decimal("40000"),
                "",
                ("buy-and-hold", "exchange"),
                "buy-and-hold",
                "exchange",
            ),
            UtxoHolding(
                "cold",
                "C",
                1,
                Decimal("0.5"),
                Decimal("20000"),
                "mixed",
                Decimal("12500"),
                "",
                ("accumulating", "exchange", "mixed"),
                "accumulating",
                "exchange",
            ),
        ],
        fiat=[FiatHolding("bank", Decimal("12500"), "buffer")],
        current_price_eur=PRICE,
    )


def test_portfolio_coverage_is_value_weighted() -> None:
    portfolio = _portfolio()
    portfolio.utxos[0].coverage_share = Decimal("0.5")
    portfolio.utxos[1].coverage_share = Decimal("1")
    assert portfolio.coverage_value_eur == Decimal("60000")
    assert portfolio.coverage_share == Decimal("0.6")


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


def test_overview_renders_value_weighted_coverage(tmp_path: Path) -> None:
    from utxoproof.reports import write_overview_page

    portfolio = _portfolio()
    portfolio.utxos[0].coverage_share = Decimal("0.5")
    portfolio.utxos[1].coverage_share = Decimal("1")
    target = write_overview_page(portfolio, "2024-06-01", "test price", tmp_path, "")
    html = target.read_text(encoding="utf-8")
    assert "bank-proven BTC value" in html
    assert "60%" in html


def test_overview_hub_lists_related_reports(tmp_path: Path) -> None:
    from utxoproof.reports import write_overview_page

    target = write_overview_page(
        _portfolio(),
        "2024-06-01",
        "test price",
        tmp_path,
        "",
        reports=[
            {"title": "Tax report 2023", "href": "../../2023/report.html"},
            {"title": "Bank review", "href": "../../bank/bank.html"},
        ],
    )
    html = target.read_text(encoding="utf-8")
    assert "Related reports" in html
    assert "../../2023/report.html" in html
    assert "../../bank/bank.html" in html


def test_entity_review_link_renders(tmp_path: Path) -> None:
    from utxoproof.reports import write_entity_pages

    write_entity_pages(
        _portfolio(),
        "../provenance",
        tmp_path,
        review_links={"bank": {"title": "Bank review", "href": "../../bank/bank.html"}},
    )
    bank = (tmp_path / "bank.html").read_text(encoding="utf-8")
    assert "Review:" in bank and "../../bank/bank.html" in bank
    cold = (tmp_path / "cold.html").read_text(encoding="utf-8")
    assert "Review:" not in cold


def test_behavior_and_channel_splits_sum_to_total() -> None:
    from utxoproof.portfolio import Entity, UtxoHolding

    portfolio = Portfolio(
        entities=[Entity("w", "wallet", "W")],
        utxos=[
            UtxoHolding(
                "w",
                "a",
                0,
                Decimal("1"),
                Decimal("40000"),
                "kyc",
                Decimal("0"),
                "",
                (),
                "buy-and-hold",
                "exchange",
            ),
            UtxoHolding(
                "w",
                "b",
                0,
                Decimal("1"),
                Decimal("20000"),
                "kyc",
                Decimal("0"),
                "",
                (),
                "speculation",
                "exchange",
            ),
        ],
        current_price_eur=Decimal("40000"),
    )
    assert portfolio.behavior_split() == [
        ("speculation", Decimal("20000")),
        ("buy-and-hold", Decimal("40000")),
    ]
    assert portfolio.channel_split() == [("exchange", Decimal("60000"))]
    assert sum(v for _, v in portfolio.behavior_split()) == portfolio.btc_value_eur


def test_portfolio_from_db_tags_holdings(tmp_path: Path) -> None:
    import datetime

    from utxoproof.kyc import create_sample_graph
    from utxoproof.portfolio import Entity, portfolio_from_db

    db = create_sample_graph()
    entities = [Entity("w", "wallet", "W", wallet="w", tags=("bank-transfer",))]
    portfolio, _ = portfolio_from_db(
        db,
        entities,
        [],
        "w",
        lambda _d: Decimal("20000"),
        Decimal("40000"),
        datetime.date(2024, 6, 1),
        {"D:0": ("cash",)},
    )
    assert portfolio.utxos, "sample graph must yield holdings"
    for u in portfolio.utxos:
        assert u.behavior in ("speculation", "buy-and-hold", "accumulating")
        assert u.channel in ("bank-transfer", "exchange", "mining", "unknown")
        assert "bank-transfer" in u.tags  # entity inheritance
    d0 = next(u for u in portfolio.utxos if (u.txid, u.vout) == ("D", 0))
    assert "cash" in d0.tags  # per-UTXO override
    assert d0.channel == "bank-transfer"  # asserted channel wins the bucket


def test_overview_renders_behavior_and_channel_donuts(tmp_path: Path) -> None:
    from utxoproof.reports import write_overview_page

    target = write_overview_page(_portfolio(), "2024-06-01", "test price", tmp_path, "")
    html = target.read_text(encoding="utf-8")
    assert "Holdings by tax behavior" in html
    assert "Holdings by funding channel" in html
    assert html.count("conic-gradient") >= 3  # kyc + behavior + channel
    assert "Related reports" not in html  # omitted without siblings


def _labeled_db() -> object:
    db = _sample_db_with_spendable()
    db.execute("UPDATE tx_outputs SET wallet_label='cold' WHERE txid='C' AND vout=1")
    db.execute("UPDATE tx_outputs SET wallet_label='hot' WHERE txid='D' AND vout=0")
    db.commit()
    return db


def _two_wallet_entities() -> list:
    from utxoproof.portfolio import Entity

    return [
        Entity("cold", "wallet", "Cold", "", wallet="cold"),
        Entity("hot", "wallet", "Hot", "", wallet="hot"),
    ]


def test_portfolio_splits_unspent_across_wallet_entities() -> None:
    import datetime

    from utxoproof.portfolio import portfolio_from_db

    db = _labeled_db()
    portfolio, _ = portfolio_from_db(
        db,
        _two_wallet_entities(),
        [],
        "cold",
        lambda _day: Decimal("20000"),
        Decimal("40000"),
        datetime.date(2024, 6, 1),
    )
    by_utxo = {f"{u.txid}:{u.vout}": u.entity_id for u in portfolio.utxos}
    assert by_utxo == {"C:1": "cold", "D:0": "hot"}
    assert {u.entity_id for u in portfolio.utxos} == {"cold", "hot"}
    total = sum((u.eur_value for u in portfolio.utxos), Decimal("0"))
    assert total == portfolio.btc_value_eur  # each UTXO counted exactly once


def test_portfolio_shared_output_picks_first_sorted_entity() -> None:
    import datetime

    from utxoproof.portfolio import portfolio_from_db

    db = _labeled_db()
    db.execute("UPDATE tx_outputs SET wallet_label='hot,cold' WHERE txid='D' AND vout=0")
    db.commit()
    portfolio, _ = portfolio_from_db(
        db,
        _two_wallet_entities(),
        [],
        "cold",
        lambda _day: Decimal("20000"),
        Decimal("40000"),
        datetime.date(2024, 6, 1),
    )
    by_utxo = {f"{u.txid}:{u.vout}": u.entity_id for u in portfolio.utxos}
    assert by_utxo["D:0"] == "cold"


def test_portfolio_unmapped_wallet_raises_naming_it() -> None:
    import datetime

    import pytest

    from utxoproof.portfolio import portfolio_from_db

    db = _labeled_db()
    db.execute("UPDATE tx_outputs SET wallet_label='ghost' WHERE txid='D' AND vout=0")
    db.commit()
    with pytest.raises(ValueError, match="ghost"):
        portfolio_from_db(
            db,
            _two_wallet_entities(),
            [],
            "cold",
            lambda _day: Decimal("20000"),
            Decimal("40000"),
            datetime.date(2024, 6, 1),
        )


def _fiat_entities(
    tmp_path: Path, fiat_block: str, statements: dict[str, str] | None = None
) -> Path:
    cfg = tmp_path / "entities.toml"
    body = (
        '[[entities]]\nid = "sav"\nkind = "bank"\nlabel = "Savings"\n'
        + (f"statements = [{', '.join(repr(s) for s in statements)}]\n" if statements else "")
        + fiat_block
    )
    cfg.write_text(body, encoding="utf-8")
    if statements:
        for name, content in statements.items():
            (tmp_path / name).write_text(content, encoding="utf-8")
    return cfg


def test_fiat_check_fresh_and_evidenced_is_quiet(tmp_path: Path, capsys) -> None:
    import datetime

    from utxoproof.cli import main

    today = datetime.datetime.now(datetime.UTC).date()
    cfg = _fiat_entities(
        tmp_path,
        '[[fiat]]\nentity_id = "sav"\namount_eur = "100"\nnote = "x"\n'
        f'as_of = "{today.isoformat()}"\n',
        {"stmt.csv": "a;b\n"},
    )
    assert main(["fiat-check", "--entities", str(cfg)]) == 0
    out = capsys.readouterr().out
    assert "checked 1 holding(s), 0 warning(s)" in out


def test_fiat_check_warns_stale_undated_unverified_and_future(tmp_path: Path, capsys) -> None:
    import datetime

    from utxoproof.cli import main

    old = (datetime.datetime.now(datetime.UTC).date() - datetime.timedelta(days=100)).isoformat()
    future = (datetime.datetime.now(datetime.UTC).date() + datetime.timedelta(days=5)).isoformat()
    today = datetime.datetime.now(datetime.UTC).date().isoformat()
    cfg = tmp_path / "entities.toml"
    cfg.write_text(
        '[[entities]]\nid = "a"\nkind = "bank"\nlabel = "A"\n'
        '[[entities]]\nid = "b"\nkind = "bank"\nlabel = "B"\n'
        '[[entities]]\nid = "c"\nkind = "bank"\nlabel = "C"\n'
        f'[[fiat]]\nentity_id = "a"\namount_eur = "1"\nas_of = "{old}"\n'
        '[[fiat]]\nentity_id = "b"\namount_eur = "2"\n'
        f'[[fiat]]\nentity_id = "c"\namount_eur = "3"\nas_of = "{future}"\n',
        encoding="utf-8",
    )
    assert main(["fiat-check", "--entities", str(cfg), "--as-of", today]) == 0
    out = capsys.readouterr().out
    assert "100 days old" in out and "no as-of date" in out and "in the future" in out
    assert "no statement file" in out  # all three lack statements
    assert "checked 3 holding(s), 6 warning(s)" in out


def test_fiat_check_missing_statement_file_warns(tmp_path: Path, capsys) -> None:
    import datetime

    from utxoproof.cli import main

    cfg = tmp_path / "entities.toml"
    today = datetime.datetime.now(datetime.UTC).date().isoformat()
    cfg.write_text(
        '[[entities]]\nid = "sav"\nkind = "bank"\nlabel = "S"\nstatements = ["gone.csv"]\n'
        f'[[fiat]]\nentity_id = "sav"\namount_eur = "1"\nas_of = "{today}"\n',
        encoding="utf-8",
    )
    assert main(["fiat-check", "--entities", str(cfg)]) == 0
    assert "not found: gone.csv" in capsys.readouterr().out


def test_fiat_malformed_as_of_raises(tmp_path: Path) -> None:
    import pytest

    from utxoproof.portfolio import load_entities

    cfg = tmp_path / "entities.toml"
    cfg.write_text(
        '[[entities]]\nid = "sav"\n'
        '[[fiat]]\nentity_id = "sav"\namount_eur = "1"\nas_of = "06/01/2024"\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="must be YYYY-MM-DD"):
        load_entities(cfg)
