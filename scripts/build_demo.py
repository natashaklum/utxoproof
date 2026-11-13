"""Build the Pages demo site: reports, v8 example and Getting Started guide.

Usage: ``python scripts/build_demo.py --out site``. Reads only repo fixtures,
``examples/input_v8`` and the static guide under ``docs/guide``; writes
self-contained HTML (no external assets). The v8 demo runs offline and seeds a
small deterministic chain graph.
"""

from __future__ import annotations

import argparse
import csv
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path

from utxoproof.accounts import Account, Registry, attribute_outputs, load_registry
from utxoproof.db import relink_spends
from utxoproof.kraken_csv import parse_kraken_ledgers, to_manual_csv_rows
from utxoproof.kyc import create_sample_graph
from utxoproof.reports import (
    write_descriptors_page,
    write_privacy_page,
    write_report,
    write_status_page,
)

ROOT = Path(__file__).resolve().parent.parent
V8_INPUT = ROOT / "examples" / "input_v8"
V8_NOTICE = (
    "DEMO \u2014 synthetic v8 account workflow. Chain transactions are seeded "
    "for illustration; not real holdings or tax advice."
)


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


def _v8_script(account: Account, chain: int, index: int) -> tuple[str, str]:
    """First-party descriptor address/script for the demo chain fixture."""
    from utxoproof.descriptors import derive_descriptor_scripts

    descriptor = account.descriptors["external" if chain == 0 else "change"]
    address, script_hex = derive_descriptor_scripts(
        descriptor, branch=0, start=index, count=1, network=account.network
    )[0]
    return address, script_hex


def _seed_v8_chain(db: sqlite3.Connection, registry: Registry) -> None:
    """Insert a small deterministic graph that exercises every account page.

    Txids 1111/2222 match the BIP329 example labels. 4444 spends one ledger
    output and pays sparrow change, so provenance and spent-output handling are
    visible without any network access.
    """
    transactions = [
        ("1" * 64, 665_000, "2021-01-15T12:00:00+00:00"),
        ("2" * 64, 700_000, "2022-02-02T12:00:00+00:00"),
        ("3" * 64, 720_000, "2022-09-09T12:00:00+00:00"),
        ("4" * 64, 760_000, "2023-03-04T12:00:00+00:00"),
        ("a1" * 32, 630_000, "2020-05-01T09:00:00+00:00"),
        ("b1" * 32, 790_000, "2023-02-27T10:30:00+00:00"),
        ("b2" * 32, 795_000, "2023-03-03T15:00:00+00:00"),
    ]
    db.executemany(
        "INSERT INTO transactions (txid,block_height,block_time,is_coinbase) VALUES (?,?,?,0)",
        transactions,
    )

    def output(
        txid: str,
        vout: int,
        account_id: str,
        chain: int,
        index: int,
        value_sat: int,
        source_type: str | None,
    ) -> None:
        account = registry.get(account_id)
        address, script_hex = _v8_script(account, chain, index)
        db.execute(
            "INSERT INTO tx_outputs "
            "(txid,vout,address,value_sat,source_type,source_label,script_hex,"
            "is_mine,account_id) VALUES (?,?,?,?,?,?,?,1,?)",
            (
                txid,
                vout,
                address,
                value_sat,
                source_type,
                "demo exchange purchase" if source_type else None,
                script_hex,
                account_id,
            ),
        )

    output("1" * 64, 0, "ledger-main", 0, 0, 50_000_000, "exchange_purchase")
    output("1" * 64, 1, "ledger-main", 0, 1, 25_000_000, "exchange_purchase")
    output("2" * 64, 0, "sparrow-savings", 0, 0, 75_000_000, "exchange_purchase")
    output("3" * 64, 0, "multisig-vault", 0, 0, 100_000_000, "exchange_purchase")
    output("4" * 64, 0, "sparrow-savings", 1, 0, 24_000_000, None)
    output("a1" * 32, 0, "bisq-wallet-legacy", 0, 0, 3_000_000, "p2p_purchase")
    output("b1" * 32, 0, "bisq-wallet-segwit", 0, 0, 1_234_000, "p2p_purchase")
    output("b2" * 32, 1, "bisq-wallet-segwit", 1, 0, 730_000, None)
    db.execute(
        "INSERT INTO tx_outputs "
        "(txid,vout,address,value_sat,script_hex,is_mine,account_id) VALUES (?,?,?,?,?,0,NULL)",
        (
            "b2" * 32,
            0,
            "bc1qpux3z758ulsxg69eptaakukraanqwtdxe5yy4c",
            500_000,
            "",
        ),
    )
    db.execute(
        "INSERT INTO tx_inputs (txid,input_index,prev_txid,prev_vout) VALUES (?,?,?,?)",
        ("b2" * 32, 0, "b1" * 32, 0),
    )
    db.execute(
        "INSERT INTO tx_outputs "
        "(txid,vout,address,value_sat,script_hex,is_mine,account_id) VALUES (?,?,?,?,?,0,NULL)",
        (
            "4" * 64,
            1,
            "bc1qcr8te4kr609gcawutmrza0j4xv80jy8z306fyu",
            900_000,
            "0014b71e0bbf787bc875d9f3d50ca581d558ff00f6cf",
        ),
    )
    db.execute(
        "INSERT INTO tx_inputs (txid,input_index,prev_txid,prev_vout) VALUES (?,?,?,?)",
        ("4" * 64, 0, "1" * 64, 1),
    )
    relink_spends(db)
    attribute_outputs(db, registry, strict=True)


def _build_v8_demo(out: Path) -> list[tuple[str, str]]:
    """Run the v8 example offline, seed a graph, and copy the rendered site."""
    from utxoproof.runner import RunOptions, run

    with tempfile.TemporaryDirectory(prefix="utxoproof-v8-demo-") as temp:
        data = Path(temp) / "data"

        def options(only: tuple[str, ...] = ()) -> RunOptions:
            return RunOptions(
                input_dir=V8_INPUT,
                data_dir=data,
                offline=True,
                year=2023,
                as_of="2023-12-31",
                no_zip=True,
                demo_notice=V8_NOTICE,
                only=only,
                nav_extra=(
                    ("Demo home", "../index.html"),
                    ("Getting Started guide", "../guide/index.html"),
                ),
            )

        if run(options()) != 0:
            raise RuntimeError("v8 example run failed")
        registry = load_registry(V8_INPUT)
        db = sqlite3.connect(data / "utxoproof.db")
        try:
            _seed_v8_chain(db, registry)
        finally:
            db.close()
        if run(options(("report", "portfolio"))) != 0:
            raise RuntimeError("v8 example report refresh failed")
        shutil.copytree(data / "reports", out / "v8", dirs_exist_ok=True)

    landing = out / "v8" / "index.html"
    landing.write_text(
        '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
        '<meta http-equiv="refresh" content="0; url=overview/overview.html">'
        "<title>utxoproof v8 demo</title></head><body>"
        '<div style="background:#fff3cd;border:2px solid #e8a13d;border-radius:8px;'
        'padding:0.8em 1em;margin:1em 0;font-weight:bold;text-align:center;">'
        + V8_NOTICE
        + '</div><p><a href="overview/overview.html">Open the v8 account dashboard</a> '
        '| <a href="../index.html">Back to all demo reports</a></p></body></html>',
        encoding="utf-8",
    )
    return [
        ("v8 account workflow overview", "v8/overview/overview.html"),
        ("v8 Ledger main account", "v8/entities/ledger-main.html"),
        ("v8 bank review", "v8/bank/bank.html"),
        ("v8 exchange review", "v8/exchange/index.html"),
        ("v8 Bisq trades + SEPA receipts", "v8/exchange/bisq.html"),
        ("v8 Bisq legacy wallet", "v8/entities/bisq-wallet-legacy.html"),
        ("v8 Bisq segwit wallet", "v8/entities/bisq-wallet-segwit.html"),
        ("v8 manual old-exchange events", "v8/exchange/old-exchange.html"),
        ("v8 tax report 2023", "v8/2023/report.html"),
    ]


INDEX_TEMPLATE = (ROOT / "scripts" / "demo_index.html").read_text(encoding="utf-8")


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
        ("Demo home", "index.html"),
        ("Getting Started guide", "guide/index.html"),
        ("Full report (everything)", "full/fullreport.html"),
        ("Portfolio overview", "overview/overview.html"),
    ]
    for _demo in DEMOS:
        nav_pages.append((_demo.title, f"{_demo.slug}/report.html"))
        nav_pages.append((f"Holdings status — {_demo.slug}", f"{_demo.slug}/status.html"))
    nav_pages += [
        ("v8 account workflow overview", "v8/overview/overview.html"),
        ("v8 Ledger main account", "v8/entities/ledger-main.html"),
        ("v8 bank review", "v8/bank/bank.html"),
        ("v8 exchange review", "v8/exchange/index.html"),
        ("v8 tax report 2023", "v8/2023/report.html"),
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

    for offset, (title, href) in enumerate(_build_v8_demo(out), start=2):
        links.insert(offset, f'<li><a href="{href}">{title}</a></li>')

    _write_guide(out)
    _write_docs_page(out)
    if (out / "docs" / "usage.html").is_file():
        links.append('<li><a href="docs/usage.html">User guide (Slowstart)</a></li>')
    (out / "index.html").write_text(
        INDEX_TEMPLATE.replace("{links}", "\n".join(links)), encoding="utf-8"
    )
    print(f"demo site: {out} ({len(links)} reports)")
    return 0


def _write_guide(out: Path) -> None:
    """Copy the hand-built Getting Started guide into the published site."""
    source = ROOT / "docs" / "guide"
    if not (source / "index.html").is_file():
        return
    shutil.copytree(source, out / "guide", dirs_exist_ok=True)


def _write_docs_page(out: Path) -> None:
    """Render docs/USAGE.md as site/docs/usage.html.

    The illustrated Getting Started guide is static HTML; the deeper reference
    needs ``markdown``. If the optional library is absent, keep a small local
    pointer instead of a dangling nav link.
    """
    source = ROOT / "docs" / "USAGE.md"
    try:
        import markdown
    except ImportError:
        body = (
            "<h1>utxoproof user guide</h1>"
            "<p>The full reference is available in the repository: "
            '<a href="https://github.com/natashaklum/utxoproof/blob/main/docs/USAGE.md">'
            "docs/USAGE.md</a>.</p>"
        )
    else:
        if not source.is_file():
            return
        body = markdown.markdown(
            source.read_text(encoding="utf-8"), extensions=["tables", "fenced_code"]
        )
    css = (
        ".docs-main{max-width:980px;margin:0 auto;padding:34px 28px 88px;}"
        ".docs-main h1{margin:12px 0 18px;font-family:var(--serif);"
        "font-size:clamp(2.4rem,5vw,3.8rem);font-weight:600;line-height:1;"
        "letter-spacing:-.04em;}"
        ".docs-main h2{font-family:var(--serif);font-size:clamp(1.7rem,3vw,2.3rem);"
        "font-weight:600;letter-spacing:-.03em;}"
        ".docs-main h3{margin-top:30px;}"
        ".docs-main table{margin:18px 0;border:1px solid var(--line);"
        "border-radius:14px;background:var(--card);overflow:hidden;}"
        ".docs-main th{padding:11px 13px;background:#f4f1eb;color:var(--muted);"
        "font-size:10px;font-weight:750;letter-spacing:.06em;text-align:left;"
        "text-transform:uppercase;}"
        ".docs-main td{padding:12px 13px;border-top:1px solid var(--line);"
        "vertical-align:top;}"
        ".docs-main pre{font-size:12px;}"
        ".docs-main code{word-break:break-word;}"
        ".demo-banner{margin:0 0 24px;padding:13px 17px;border:1px solid #f3d3a5;"
        "border-left:4px solid var(--orange);border-radius:0 13px 13px 0;"
        "background:var(--orange-soft);color:#56320d;font-size:13px;font-weight:700;}"
        ".guide-banner{margin:0 0 30px;padding:14px 18px;border:1px solid #b7e4c8;"
        "border-radius:13px;background:var(--green-soft);color:#0b4c32;font-size:13.5px;}"
        "@media(max-width:680px){.docs-main{padding:26px 18px 70px;}}"
    )
    banner = (
        '<div class="demo-banner">DEMO \u2014 docs for the demo site; '
        "commands shown act on your own files.</div>"
        '<div class="guide-banner">New to the v8 workflow with a lot of data? '
        '<a href="../guide/index.html">Read the illustrated Getting Started guide</a>.</div>'
    )
    page = [
        "<!DOCTYPE html>",
        '<html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        "<title>utxoproof user guide</title>",
        '<link rel="stylesheet" href="../guide/style.css">',
        f"<style>{css}</style></head><body>",
        '<a class="skip-link" href="#docs">Skip to the reference</a>',
        '<header class="topbar"><div class="topbar-inner">'
        '<a class="brand" href="../index.html"><span class="brand-mark" aria-hidden="true">₿</span>'
        '<span>utxoproof</span><span class="brand-sub">reference</span></a>'
        '<nav class="topnav" aria-label="Site links">'
        '<a href="../guide/index.html">Getting Started</a>'
        '<a href="../v8/overview/overview.html">Live dashboard</a>'
        '<a href="../index.html">Demo home</a>'
        '<a class="github" href="https://github.com/natashaklum/utxoproof">GitHub '
        '<span aria-hidden="true">↗</span></a></nav></div></header>',
        '<main id="docs" class="docs-main">',
        banner,
        body,
        "</main></body></html>",
    ]
    docs = out / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    (docs / "usage.html").write_text("\n".join(page), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
