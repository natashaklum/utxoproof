"""Build the Pages demo site: HTML reports from fixture data + index.

Usage: ``python scripts/build_demo.py --out site``. Reads only files under
``tests/fixtures``; writes self-contained HTML (no external assets).
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from pathlib import Path

from utxoproof.kraken_csv import parse_kraken_ledgers, to_manual_csv_rows
from utxoproof.kyc import create_sample_graph
from utxoproof.reports import (
    write_descriptors_page,
    write_privacy_page,
    write_report,
    write_status_page,
)

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Demo:
    slug: str
    title: str
    csv_path: Path | None
    year: int
    converter: str = "manual"  # manual | kraken | coinbase


DEMOS: list[Demo] = [
    Demo(
        slug="manual-2023",
        title="Manual CSV — tax year 2023",
        csv_path=ROOT / "tests" / "fixtures" / "manual_2023.csv",
        year=2023,
    ),
    Demo(
        slug="kraken-2023",
        title="Kraken ledgers.csv import — tax year 2023",
        csv_path=None,  # converted from kraken_ledgers_2023.csv below
        year=2023,
        converter="kraken",
    ),
    Demo(
        slug="coinbase-2023",
        title="Coinbase history import — tax year 2023",
        csv_path=None,  # converted from coinbase_2023.csv below
        year=2023,
        converter="coinbase",
    ),
]

DEMO_NOTICE = (
    "DEMO \u2014 synthetic dummy data for illustration only. "
    "Not real transactions; figures are meaningless for filing."
)

INDEX_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><title>utxoproof demo reports</title></head>
<body>
<h1>utxoproof demo reports</h1>
<div class="demo-banner">DEMO \u2014 dummy data on every page. Not real.</div>
<style>.demo-banner { background: #fff3cd; border: 2px solid #e8a13d;
border-radius: 8px; padding: 0.8em 1em; margin: 1em 0;
font-weight: bold; text-align: center; }</style>
<ul>
{links}
</ul>
</body>
</html>
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True, help="Output site directory")
    args = parser.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    links: list[str] = []

    # Predetermined nav: the demo layout is fully known upfront, so every page
    # gets the same menu (no existence gating needed mid-build).
    from utxoproof.reports import site_nav

    nav_pages: list[tuple[str, str]] = [
        ("Full report (everything)", "full/fullreport.html"),
        ("Portfolio overview", "overview/overview.html"),
    ]
    for _demo in DEMOS:
        nav_pages.append((_demo.title, f"{_demo.slug}/report.html"))
        nav_pages.append((f"Holdings status — {_demo.slug}", f"{_demo.slug}/status.html"))
    nav_pages += [
        ("Descriptor check (BIP84 vectors)", "descriptors/descriptors.html"),
        ("Bank review (KBC statement)", "bank/bank.html"),
        ("Privacy report (sample graph)", "privacy/privacy.html"),
        ("Advisory (sample graph)", "advisory/advisory.html"),
        ("Provenance D:0", "provenance/provenance_D_0.html"),
        ("All-time summary (manual CSV)", "alltime/summary.html"),
    ]

    def dnav(target_rel: str) -> list[dict[str, object]]:
        return site_nav(nav_pages, out, out / target_rel)

    # Receipt-date valuation for deposits, from the vendored price history
    # (offline, deterministic).
    from utxoproof.db import open_memory_db
    from utxoproof.price_oracle import EURPriceOracle

    _history_db = open_memory_db()
    _history_oracle = EURPriceOracle(_history_db)
    _history_oracle.load_csv(
        ROOT / "utxoproof" / "data" / "btc_eur_daily.csv", "BTC/EUR", "demo-seed"
    )
    demo_price_at = _history_oracle.get_btc_eur

    for demo in DEMOS:
        slug = demo.slug
        year = demo.year
        if demo.csv_path is None:
            if demo.converter == "coinbase":
                from utxoproof.coinbase_csv import parse_coinbase_csv

                txs = parse_coinbase_csv(ROOT / "tests" / "fixtures" / "coinbase_2023.csv")
            else:
                txs = parse_kraken_ledgers(ROOT / "tests" / "fixtures" / "kraken_ledgers_2023.csv")
            rows = to_manual_csv_rows(txs)
            csv_path = out / f"{slug}.csv"
            with open(csv_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(
                    f,
                    fieldnames=[
                        "date",
                        "side",
                        "kind",
                        "trade_refs",
                        "btc",
                        "eur_per_btc",
                        "fee_eur",
                    ],
                )
                writer.writeheader()
                writer.writerows(rows)
        else:
            csv_path = demo.csv_path
        target = write_report(
            str(csv_path),
            year,
            out / slug,
            demo_notice=DEMO_NOTICE,
            price_at=demo_price_at,
            nav=dnav(f"{slug}/report.html"),
        )
        rel = target.relative_to(out)
        links.append(f'<li><a href="{rel}">{demo.title}</a></li>')

    # Bank review demo: KBC statement rows parsed for real, matched against the
    # demo Kraken trades. The kbc-checking entity below carries the statement
    # inflow total, labeled as such — a checking balance cannot be derived from
    # a partial statement (no opening balance), so real setups hand-assert it.
    from utxoproof.banks import parse_bank_csv
    from utxoproof.matching import match_fiat_legs
    from utxoproof.reports import write_bank_page

    kraken_demo_txs = parse_kraken_ledgers(ROOT / "tests" / "fixtures" / "kraken_ledgers_2023.csv")
    kbc_rows = parse_bank_csv(ROOT / "tests" / "fixtures" / "kbc_2023.csv", "kbc")
    kbc_matches, kbc_unmatched = match_fiat_legs(kraken_demo_txs, kbc_rows)
    write_bank_page(
        "kbc",
        kbc_rows,
        kbc_matches,
        kbc_unmatched,
        out / "bank",
        demo_notice=DEMO_NOTICE,
        nav=dnav("bank/bank.html"),
    )
    links.append('<li><a href="bank/bank.html">Bank review (KBC statement)</a></li>')

    write_descriptors_page(
        out / "descriptors", demo_notice=DEMO_NOTICE, nav=dnav("descriptors/descriptors.html")
    )
    links.append(
        '<li><a href="descriptors/descriptors.html">Descriptor check (BIP84 vectors)</a></li>'
    )

    # Holdings status at a fixed, labeled demo price (deterministic output).
    from decimal import Decimal

    demo_price = Decimal("40000")
    price_note = "fixed demo price 40000 EUR/BTC"
    for slug, csv_path in (
        ("manual-2023", ROOT / "tests" / "fixtures" / "manual_2023.csv"),
        ("kraken-2023", out / "kraken-2023.csv"),
        ("coinbase-2023", out / "coinbase-2023.csv"),
    ):
        write_status_page(
            csv_path,
            demo_price,
            price_note,
            out / slug,
            demo_notice=DEMO_NOTICE,
            price_at=demo_price_at,
            nav=dnav(f"{slug}/status.html"),
        )
        links.append(f'<li><a href="{slug}/status.html">Holdings status — {slug}</a></li>')

    # Privacy analysis over the shared sample graph (in-memory demo DB).
    write_privacy_page(
        create_sample_graph(),
        out / "privacy",
        demo_notice=DEMO_NOTICE,
        nav=dnav("privacy/privacy.html"),
    )
    links.append('<li><a href="privacy/privacy.html">Privacy report (sample graph)</a></li>')

    # Advisory over the sample graph + one seasoned estate UTXO.
    import datetime
    from decimal import Decimal as _Decimal

    from utxoproof.advisory import analyze_wallet
    from utxoproof.kyc import propagate_graph, seed_source_kyc
    from utxoproof.reports import write_advisory_page

    demo_db = create_sample_graph()
    demo_db.execute("INSERT INTO transactions (txid, block_time) VALUES ('E', '2021-06-01')")
    demo_db.execute(
        "INSERT INTO tx_outputs (txid, vout, value_sat, source_type) "
        "VALUES ('E', 0, 200000000, 'exchange_purchase')"
    )
    demo_db.commit()
    seed_source_kyc(demo_db)
    propagate_graph(demo_db)

    # Synthetic cross-domain links so the demo provenance page exercises the
    # bank -> exchange -> chain saga. These are demo-only values, not fixtures.
    from utxoproof.banks import BankRow
    from utxoproof.exchange import ExchangeTx, FiatFlow
    from utxoproof.saga import SagaSources

    demo_saga_sources = SagaSources(
        transactions=[
            ExchangeTx(
                datetime.date(2023, 1, 2),
                "BUY",
                _Decimal("1"),
                _Decimal("20000"),
                _Decimal("0"),
                "DEMO-T1",
                exchange="kraken",
                source_evidence="demo-kraken-ledgers.csv:refid:DEMO-T1",
            ),
            ExchangeTx(
                datetime.date(2023, 1, 3),
                "WITHDRAWAL",
                _Decimal("1"),
                _Decimal("0"),
                _Decimal("0"),
                "DEMO-W1",
                exchange="kraken",
                source_evidence="demo-kraken-ledgers.csv:refid:DEMO-W1",
            ),
        ],
        fiat_flows=[
            FiatFlow(
                datetime.date(2022, 12, 30),
                "DEPOSIT",
                _Decimal("20000"),
                "DEMO-D1",
                source_evidence="demo-kraken-ledgers.csv:refid:DEMO-D1",
            )
        ],
        bank_rows=[
            BankRow(
                datetime.date(2022, 12, 30),
                "Kraken deposit",
                _Decimal("-20000"),
                None,
                "DEMO-D1",
                source_file="demo-kraken-bank.csv",
                row_number=2,
            )
        ],
        ledgers_file="demo-kraken-ledgers.csv",
        bank_files=("demo-kraken-bank.csv",),
    )
    curve = {
        datetime.date(2021, 6, 1): _Decimal("5000"),
        datetime.date(2023, 1, 1): _Decimal("20000"),
        datetime.date(2023, 1, 2): _Decimal("21000"),
        datetime.date(2023, 2, 1): _Decimal("25000"),
        datetime.date(2023, 3, 1): _Decimal("30000"),
    }
    as_of = datetime.date(2024, 6, 1)
    advisories = analyze_wallet(demo_db, lambda day: curve[day], _Decimal("40000"), as_of)
    write_advisory_page(
        advisories,
        _Decimal("40000"),
        "illustrative demo curve",
        as_of.isoformat(),
        out / "advisory",
        demo_notice=DEMO_NOTICE,
        nav=dnav("advisory/advisory.html"),
    )
    links.append('<li><a href="advisory/advisory.html">Advisory (sample graph)</a></li>')

    # Provenance chain for D:0 over the same demo DB, plus one dummy
    # attachment so the appendix renders on the demo site.
    import binascii
    import struct
    import zlib

    from utxoproof.evidence import attach_file
    from utxoproof.reports import write_provenance_page

    def _dummy_png() -> bytes:
        def chunk(typ: bytes, data: bytes) -> bytes:
            body = struct.pack(">I", len(data)) + typ + data
            return body + struct.pack(">I", binascii.crc32(typ + data) & 0xFFFFFFFF)

        ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
        return (
            b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(b"\x00\xc8\x1e\x1e"))
            + chunk(b"IEND", b"")
        )

    _dummy_src = out / "dummy-confirmation.png"
    _dummy_src.write_bytes(_dummy_png())
    attach_file(
        demo_db,
        _dummy_src,
        out / "demo-evidence",
        2023,
        txid="C",
        note="dummy withdrawal confirmation",
    )
    _dummy_src.unlink()

    write_provenance_page(
        demo_db,
        "D",
        0,
        lambda day: curve[day],
        _Decimal("40000"),
        as_of,
        out / "provenance",
        demo_notice=DEMO_NOTICE,
        evidence_root=out / "demo-evidence",
        evidence_url_prefix="../demo-evidence",
        nav=dnav("provenance/provenance_D_0.html"),
        saga_sources=demo_saga_sources,
    )
    links.append('<li><a href="provenance/provenance_D_0.html">Provenance D:0</a></li>')

    from utxoproof.reports import write_alltime_page

    write_alltime_page(
        ROOT / "tests" / "fixtures" / "manual_2023.csv",
        out / "alltime",
        demo_notice=DEMO_NOTICE,
        price_at=demo_price_at,
        nav=dnav("alltime/summary.html"),
    )
    links.append('<li><a href="alltime/summary.html">All-time summary (manual CSV)</a></li>')

    # Portfolio dashboard: entities -> UTXOs -> provenance drill-down.
    from utxoproof.portfolio import Entity, FiatHolding, Portfolio, UtxoHolding
    from utxoproof.reports import write_entity_pages, write_overview_page
    from utxoproof.reports import write_provenance_page as _write_prov

    for utxo in ("C:1", "D:0", "E:0"):
        txid, vout = utxo.split(":")
        _write_prov(
            demo_db,
            txid,
            int(vout),
            lambda day: curve[day],
            Decimal("40000"),
            as_of,
            out / "provenance",
            demo_notice=DEMO_NOTICE,
            evidence_root=out / "demo-evidence",
            evidence_url_prefix="../demo-evidence",
            nav=dnav(f"provenance/provenance_{txid}_{vout}.html"),
            saga_sources=demo_saga_sources,
        )
    unspent = demo_db.execute(
        "SELECT txid, vout, value_sat, kyc_status FROM tx_outputs WHERE spent_by_txid IS NULL"
    ).fetchall()
    advisory_by_utxo = {f"{a.txid}:{a.vout}": a for a in advisories}
    owner = {"E:0": "ledger-savings", "C:1": "phone-spending", "D:0": "phone-spending"}
    portfolio = Portfolio(
        entities=[
            Entity("ledger-savings", "wallet", "Ledger Nano — savings", "BIP84 account"),
            Entity("phone-spending", "wallet", "Phone spending", "Small hot wallet"),
            Entity("ing-savings", "bank", "ING savings", "BE68 **** 7034"),
            Entity(
                "kbc-checking",
                "bank",
                "KBC checking",
                f"statement {kbc_rows[0].date.isoformat()}-{kbc_rows[-1].date.isoformat()}, "
                f"{len(kbc_rows)} rows parsed from kbc_2023.csv",
            ),
            Entity("kraken-eur", "exchange", "Kraken EUR balance", "Uninvested euros"),
        ],
        utxos=[
            UtxoHolding(
                owner[f"{txid}:{vout}"],
                txid,
                vout,
                Decimal(value_sat) / Decimal(100_000_000),
                Decimal(value_sat) / Decimal(100_000_000) * Decimal("40000"),
                kyc_status,
                advisory_by_utxo[f"{txid}:{vout}"].acquisition_cost_eur,
            )
            for txid, vout, value_sat, kyc_status in unspent
        ],
        fiat=[
            FiatHolding("ing-savings", Decimal("12500"), "Savings buffer"),
            FiatHolding(
                "kbc-checking",
                sum((r.amount_eur for r in kbc_rows), Decimal("0")),
                "Statement inflow total — not a balance (see bank review)",
            ),
            FiatHolding("kraken-eur", Decimal("3200"), "Dry powder"),
        ],
        current_price_eur=Decimal("40000"),
    )
    from utxoproof.saga import build_saga_document

    for holding in portfolio.utxos:
        document = build_saga_document(
            demo_db, holding.txid, holding.vout, lambda day: curve[day], demo_saga_sources
        )
        holding.coverage_share = document.coverage
        holding.coverage_tier = document.weakest_tier
    from utxoproof.portfolio import load_price_series, svg_sparkline

    history = load_price_series(ROOT / "utxoproof" / "data" / "btc_eur_daily.csv")
    sparkline = svg_sparkline(history, label="BTC/EUR daily close, last 12 months")
    write_overview_page(
        portfolio,
        as_of.isoformat(),
        "fixed demo price",
        out / "overview",
        sparkline,
        demo_notice=DEMO_NOTICE,
        nav=dnav("overview/overview.html"),
    )
    flags_by_utxo = {
        utxo: ",".join(sorted(f.value for f in advisory_by_utxo[utxo].flags))
        for utxo in advisory_by_utxo
    }
    write_entity_pages(
        portfolio,
        "../provenance",
        out / "entities",
        flags_by_utxo,
        demo_notice=DEMO_NOTICE,
        nav=dnav("entities/ledger-savings.html"),
    )

    # Full printable report combining every section (same builders + macros
    # as the single pages — no duplicated markup).
    from utxoproof.reports import write_full_report

    write_full_report(
        db=demo_db,
        csv_path=ROOT / "tests" / "fixtures" / "manual_2023.csv",
        year=2023,
        out_dir=out / "full",
        price_at=lambda day: curve[day],
        current_price_eur=Decimal("40000"),
        price_note="illustrative demo curve",
        as_of=as_of,
        provenance_targets=[("C", 1), ("D", 0)],
        portfolio=portfolio,
        price_history_svg=sparkline,
        demo_notice=DEMO_NOTICE,
        evidence_root=out / "demo-evidence",
        evidence_url_prefix="../demo-evidence",
        nav=dnav("full/fullreport.html"),
        saga_sources=demo_saga_sources,
    )
    links.insert(
        0, '<li><a href="full/fullreport.html"><strong>Full report (everything)</strong></a></li>'
    )
    links.insert(
        1, '<li><a href="overview/overview.html"><strong>Portfolio overview</strong></a></li>'
    )

    _write_docs_page(out)
    if (out / "docs" / "usage.html").is_file():
        links.append('<li><a href="docs/usage.html">User guide (Slowstart)</a></li>')
    (out / "index.html").write_text(
        INDEX_TEMPLATE.replace("{links}", "\n".join(links)), encoding="utf-8"
    )
    print(f"demo site: {out} ({len(links)} reports)")
    return 0


def _write_docs_page(out: Path) -> None:
    """Render docs/USAGE.md as site/docs/usage.html (needs markdown lib)."""
    try:
        import markdown
    except ImportError:
        return
    source = ROOT / "docs" / "USAGE.md"
    if not source.is_file():
        return
    body = markdown.markdown(
        source.read_text(encoding="utf-8"), extensions=["tables", "fenced_code"]
    )
    css = (
        "body{font-family:sans-serif;max-width:900px;margin:2em auto;"
        "padding:0 1em;color:#222;}"
        "pre{background:#f6f6f6;padding:1em;overflow-x:auto;}"
        "code{word-break:break-all;}"
        ".demo-banner{background:#fff3cd;border:2px solid #e8a13d;"
        "border-radius:8px;padding:0.8em 1em;margin:1em 0;"
        "font-weight:bold;text-align:center;}"
    )
    banner = (
        '<div class="demo-banner">DEMO \u2014 docs for the demo site; '
        "commands shown act on your own files.</div>"
    )
    page = [
        "<!DOCTYPE html>",
        '<html lang="en"><head><meta charset="utf-8">',
        "<title>utxoproof user guide</title>",
        f"<style>{css}</style></head><body>",
        banner,
        body,
        "</body></html>",
    ]
    docs = out / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    (docs / "usage.html").write_text("\n".join(page), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
