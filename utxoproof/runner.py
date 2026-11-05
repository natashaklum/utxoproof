"""Account-tree discovery and the end-to-end v8 runner.

The low-level commands remain available for diagnosis. ``utxoproof run`` is
the normal entry point: validate the input tree, sync accounts, import fiat
sources, import BIP329 labels, then build the tax/full reports and dashboard.
"""

from __future__ import annotations

import csv
import datetime
import os
import sqlite3
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from utxoproof.accounts import (
    Account,
    AccountError,
    Registry,
    attribute_outputs,
    load_registry,
    output_data_dir,
    persist_registry,
    portfolio_config,
    registry_summary,
)
from utxoproof.banks import BankRow, parse_bank_csv
from utxoproof.config import Config
from utxoproof.db import init_db
from utxoproof.labels import address_labels, import_bip329, output_labels, parse_bip329
from utxoproof.saga import load_saga_sources

STEPS = ("labels", "sync", "exchanges", "banks", "report", "portfolio")


@dataclass
class RunOptions:
    input_dir: str | Path
    data_dir: str | Path
    plan: bool = False
    only: tuple[str, ...] = ()
    year: int | None = None
    offline: bool = False
    force_rescan: bool = False
    price: Decimal | None = None
    as_of: str = ""
    full: bool = False
    no_zip: bool = False
    demo_notice: str = ""
    # Extra menu entries, relative to the reports root. Used by the demo build
    # to link the generated site back to its landing page and guide; normal
    # runs leave this empty.
    nav_extra: tuple[tuple[str, str], ...] = ()


def _account_json(account: Account) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": account.id,
        "kind": account.kind,
        "label": account.label,
        "detail": account.detail,
        "tags": list(account.tags),
        "path": str(account.path or ""),
    }
    if account.kind == "wallet":
        data.update(
            {
                "network": account.network,
                "gap_limit": account.gap_limit,
                "descriptors": account.descriptors,
                "labels_file": account.labels_file,
            }
        )
    elif account.kind in ("bank", "credit_card"):
        data.update(
            {
                "profile": account.profile,
                "statements": [str(path) for path in account.statements],
                "balances": [str(balance.amount_eur) for balance in account.balances],
            }
        )
    elif account.kind == "exchange":
        data.update(
            {
                "exchange": account.exchange,
                "ledgers": [str(path) for path in account.ledgers],
                "trades": [str(path) for path in account.trades],
            }
        )
    else:
        data["holdings"] = [
            {
                "symbol": holding.symbol,
                "quantity": str(holding.quantity),
                "value_eur": str(holding.value_eur),
            }
            for holding in account.holdings
        ]
    return data


def discover(input_dir: str | Path, online: bool = False) -> dict[str, Any]:
    """Validate the input tree without writing anything (or touching the DB)."""
    try:
        registry = load_registry(input_dir)
    except AccountError as exc:
        return {
            "input": str(input_dir),
            "owner": "",
            "chain": {},
            "accounts": [],
            "warnings": [],
            "errors": [str(exc)],
            "checks": {"labels": 0, "bank_rows": 0, "exchange_txs": 0},
            "online": None,
            "ok": False,
        }
    errors = list(registry.errors)
    warnings = list(registry.warnings)
    checks: dict[str, Any] = {"labels": 0, "bank_rows": 0, "exchange_txs": 0}
    for account in registry.accounts:
        if account.kind == "wallet" and account.labels_file:
            try:
                labels, label_warnings = parse_bip329(account.labels_file)
                checks["labels"] += len(labels)
                warnings.extend(label_warnings)
            except ValueError as exc:
                errors.append(str(exc))
        elif account.kind in ("bank", "credit_card"):
            for statement in account.statements:
                try:
                    checks["bank_rows"] += len(parse_bank_csv(statement, account.profile))
                except (ValueError, OSError, csv.Error) as exc:
                    errors.append(f"{statement}: bank profile {account.profile}: {exc}")
        elif account.kind == "exchange":
            from utxoproof.kraken_csv import parse_kraken_ledgers

            for ledger in account.ledgers:
                try:
                    checks["exchange_txs"] += len(
                        parse_kraken_ledgers(ledger, account.trades[0] if account.trades else None)
                    )
                except (ValueError, OSError, csv.Error) as exc:
                    errors.append(f"{ledger}: exchange parse: {exc}")
    online_result: dict[str, Any] | None = None
    if online and registry.ok:
        try:
            online_result = _check_backend(registry.config)
        except Exception as exc:
            errors.append(f"backend check: {exc}")
    return {
        "input": str(registry.root),
        "owner": registry.config.owner.name,
        "chain": {
            "backend": registry.config.chain.backend,
            "network": registry.config.chain.network,
        },
        "accounts": [_account_json(account) for account in registry.accounts],
        "warnings": warnings,
        "errors": list(dict.fromkeys(errors)),
        "checks": checks,
        "online": online_result,
        "ok": not errors,
    }


def discover_text(input_dir: str | Path, online: bool = False) -> list[str]:
    result = discover(input_dir, online=online)
    lines = [
        f"input: {result['input']}",
        f"owner: {result['owner']}",
        f"chain: {result['chain'].get('backend', '—')} ({result['chain'].get('network', '—')})",
        f"accounts: {len(result['accounts'])}",
    ]
    for account in result["accounts"]:
        lines.append(f"  {account['id']}: {account['kind']} — {account['label']}")
    checks = result["checks"]
    lines.append(
        f"parsed: {checks['labels']} labels, {checks['bank_rows']} bank rows, "
        f"{checks['exchange_txs']} exchange txs"
    )
    if result["online"] is not None:
        lines.append(f"backend: {result['online']}")
    lines.extend(f"WARNING: {warning}" for warning in result["warnings"])
    lines.extend(f"ERROR: {error}" for error in result["errors"])
    lines.append("OK" if result["ok"] else "FAILED")
    return lines


def _check_backend(config: Config) -> dict[str, Any]:
    if config.chain.backend == "electrum":
        from utxoproof.electrum import ElectrumClient

        with ElectrumClient(
            config.chain.electrum_url, ca_file=config.chain.electrum_ca_file or None, timeout=10
        ) as client:
            version = client.call("server.version", "utxoproof", "1.4")
        return {"backend": "electrum", "server_version": version}
    if config.chain.backend == "core":
        from utxoproof.bitcoin_rpc import BitcoinRPC

        rpc = BitcoinRPC(config.chain.rpc_url, config.chain.rpc_user, config.chain.rpc_password)
        return {
            "backend": "core",
            "height": rpc.get_block_count(),
            "wallet": config.chain.core_wallet,
        }
    raise ValueError(f"unknown chain backend {config.chain.backend!r}; use electrum or core")


def _open_db(path: str | Path) -> sqlite3.Connection:
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(target))
    init_db(db)
    return db


def _selected(options: RunOptions, step: str) -> bool:
    return not options.only or step in options.only


def _validate_only(only: tuple[str, ...]) -> tuple[str, ...]:
    if not only:
        return ()
    values: list[str] = []
    for item in only:
        values.extend(part.strip() for part in item.split(",") if part.strip())
    unknown = [value for value in values if value not in STEPS]
    if unknown:
        raise ValueError(f"unknown --only step(s) {unknown}; choose from {', '.join(STEPS)}")
    return tuple(dict.fromkeys(values))


def build_run_plan(registry: Registry, options: RunOptions) -> list[dict[str, Any]]:
    selected = _validate_only(options.only)
    wallets = registry.by_kind("wallet")
    labels = sum(1 for account in wallets if account.labels_file)
    plan = [
        {
            "step": "labels",
            "selected": not selected or "labels" in selected,
            "action": f"import BIP329 labels for {labels} wallet account(s)",
        },
        {
            "step": "sync",
            "selected": (not selected or "sync" in selected)
            and not options.offline
            and bool(wallets),
            "action": (
                f"sync {len(wallets)} wallet account(s) via {registry.config.chain.backend}"
                if wallets
                else "no wallet accounts"
            ),
        },
        {
            "step": "exchanges",
            "selected": not selected or "exchanges" in selected,
            "action": f"import {len(registry.by_kind('exchange'))} exchange account(s)",
        },
        {
            "step": "banks",
            "selected": not selected or "banks" in selected,
            "action": (
                f"parse {len(registry.by_kind('bank')) + len(registry.by_kind('credit_card'))} "
                "bank/card account(s)"
            ),
        },
        {
            "step": "report",
            "selected": not selected or "report" in selected,
            "action": (
                "tax/full report for "
                f"{options.year or registry.config.reporting.tax_year or 'latest year'}"
            ),
        },
        {
            "step": "portfolio",
            "selected": not selected or "portfolio" in selected,
            "action": "overview, account and provenance pages",
        },
    ]
    return plan


def _merge_csv(files: list[Path], out: Path, unique_column: str | None = None) -> Path | None:
    """Merge same-shaped CSVs, preserving headers and rejecting duplicate keys."""
    rows: list[dict[str, str]] = []
    header: list[str] | None = None
    for path in files:
        with open(path, newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            current = list(reader.fieldnames or [])
            if header is None:
                header = current
            elif current != header:
                raise ValueError(f"{path}: columns differ from {files[0].name}")
            rows.extend(dict(row) for row in reader)
    if header is None:
        return None
    if unique_column:
        seen: dict[str, Path] = {}
        for path in files:
            with open(path, newline="", encoding="utf-8-sig") as handle:
                for row in csv.DictReader(handle):
                    key = (row.get(unique_column) or "").strip()
                    if not key:
                        continue
                    if key in seen and seen[key] != path:
                        raise ValueError(
                            f"duplicate {unique_column} {key!r} in {path} and {seen[key]}; "
                            "merged exchange ledgers need globally unique refids"
                        )
                    seen[key] = path
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=header)
        writer.writeheader()
        writer.writerows(rows)
    return out


def _write_manual(
    registry: Registry, out_dir: Path
) -> tuple[Path | None, Path | None, Path | None]:
    """Merge exchange exports and write one manual CSV for compute/report."""
    from utxoproof.exchange import to_manual_csv_rows
    from utxoproof.kraken_csv import parse_kraken_ledgers

    exchanges = registry.by_kind("exchange")
    if not exchanges:
        return None, None, None
    ledgers = [path for account in exchanges for path in account.ledgers]
    trades = [path for account in exchanges for path in account.trades]
    merged_ledgers = _merge_csv(ledgers, out_dir / "ledgers.csv", unique_column="refid")
    merged_trades = _merge_csv(trades, out_dir / "trades.csv")
    transactions = []
    for account in exchanges:
        for ledger in account.ledgers:
            account_txs = parse_kraken_ledgers(ledger, merged_trades)
            for tx in account_txs:
                tx.account_id = account.id
            transactions.extend(account_txs)
    transactions.sort(key=lambda tx: (tx.date.isoformat(), tx.refid))
    rows = to_manual_csv_rows(transactions, include_account=True)
    manual = out_dir / "manual.csv"
    fields = [
        "date",
        "side",
        "kind",
        "trade_refs",
        "btc",
        "eur_per_btc",
        "fee_eur",
        "account_id",
    ]
    with open(manual, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    return merged_ledgers, merged_trades, manual


def _write_bank_rows(
    registry: Registry, out_dir: Path
) -> tuple[Path | None, list[BankRow], list[tuple[str | Path, str]]]:
    specs: list[tuple[str | Path, str]] = []
    rows: list[BankRow] = []
    for account in [*registry.by_kind("bank"), *registry.by_kind("credit_card")]:
        for statement in account.statements:
            specs.append((statement, account.profile))
            rows.extend(parse_bank_csv(statement, account.profile, account_id=account.id))
    if not specs:
        return None, [], []
    target = out_dir / "bank-rows.csv"
    fields = [
        "date",
        "side",
        "btc",
        "eur_per_btc",
        "fee_eur",
        "description",
        "amount_eur",
        "account_id",
        "source_file",
        "row_number",
    ]
    with open(target, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "date": row.date.isoformat(),
                    "description": row.description,
                    "amount_eur": str(row.amount_eur),
                    "account_id": row.account_id,
                    "source_file": row.source_file,
                    "row_number": row.row_number,
                }
            )
    return target, rows, specs


def _import_labels(registry: Registry, db: sqlite3.Connection) -> list[str]:
    lines: list[str] = []
    for account in registry.by_kind("wallet"):
        if not account.labels_file:
            continue
        result = import_bip329(db, account.id, account.labels_file)
        lines.append(
            f"labels {account.id}: {result.imported} imported, {result.updated} updated, "
            f"{result.deleted} deleted, {result.unchanged} unchanged, "
            f"{len(result.conflicts)} conflict(s)"
        )
        lines.extend(result.warnings)
        lines.extend(result.conflicts)
    return lines


def _sync_chain(registry: Registry, db: sqlite3.Connection, options: RunOptions) -> list[str]:
    config = registry.config
    lines: list[str] = []
    wallets = registry.by_kind("wallet")
    if not wallets:
        return ["sync: no wallet accounts"]
    if options.force_rescan:
        db.execute("DELETE FROM tx_inputs")
        db.execute("DELETE FROM tx_outputs")
        db.execute("DELETE FROM transactions")
        db.execute("DELETE FROM advisory_cache")
        db.execute("DELETE FROM provenance_links WHERE origin='derived'")
        db.commit()
    if config.chain.backend == "electrum":
        from utxoproof.electrum import ElectrumClient
        from utxoproof.electrum_sync import ElectrumOnchainImporter

        with ElectrumClient(
            config.chain.electrum_url,
            ca_file=config.chain.electrum_ca_file or None,
        ) as client:
            summary = ElectrumOnchainImporter(client, db).sync_accounts(wallets)
        persist_registry(db, registry)  # extended gap limits
        attribute_outputs(db, registry, strict=True)
        lines.append(
            "electrum sync: "
            + ", ".join(f"{key}={value}" for key, value in summary.as_dict().items())
        )
        return lines
    if config.chain.backend == "core":
        from utxoproof.bitcoin_rpc import BitcoinRPC
        from utxoproof.onchain import BitcoinCoreOnchainImporter

        rpc = BitcoinRPC(config.chain.rpc_url, config.chain.rpc_user, config.chain.rpc_password)
        importer = BitcoinCoreOnchainImporter(rpc, db)
        importer.setup_wallet(config.chain.core_wallet)
        for account in wallets:
            for descriptor in account.descriptors.values():
                importer.import_descriptor(
                    config.chain.core_wallet,
                    descriptor,
                    timestamp=0 if options.force_rescan else "now",
                )
        core_summary = importer.sync(config.chain.core_wallet)
        attribute_outputs(db, registry, strict=True)
        lines.append(
            "core sync: " + ", ".join(f"{key}={value}" for key, value in core_summary.items())
        )
        return lines
    raise ValueError(f"unknown chain backend {config.chain.backend!r}; use electrum or core")


def _as_of(options: RunOptions, registry: Registry) -> datetime.date:
    raw = options.as_of or registry.config.reporting.as_of
    if raw:
        try:
            return datetime.date.fromisoformat(raw)
        except ValueError as exc:
            raise ValueError(f"as-of must be YYYY-MM-DD, got {raw!r}") from exc
    return datetime.datetime.now(datetime.UTC).date()


def _latest_bundled_date() -> datetime.date | None:
    """Last date in the offline BTC/EUR history (None if unreadable)."""
    from utxoproof.price_oracle import bundled_history_path

    path = bundled_history_path()
    try:
        with open(path, newline="", encoding="utf-8") as handle:
            dates = [row.get("date", "")[:10] for row in csv.DictReader(handle) if row.get("date")]
        return datetime.date.fromisoformat(max(dates)) if dates else None
    except (OSError, ValueError):
        return None


def _report_year(options: RunOptions, registry: Registry, manual: Path | None) -> int | None:
    if options.year:
        return options.year
    if registry.config.reporting.tax_year:
        return int(registry.config.reporting.tax_year)
    if manual is None or not manual.is_file():
        return None
    years: list[int] = []
    with open(manual, newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            try:
                years.append(int(row["date"][:4]))
            except (KeyError, ValueError):
                continue
    return max(years) if years else None


def _write_evidence(
    registry: Registry,
    db: sqlite3.Connection,
    reports_dir: Path,
    year: int,
    report_html: Path,
    manual: Path | None,
    merged_ledgers: Path | None,
    merged_trades: Path | None,
) -> Path:
    """Build the per-year evidence ZIP from input sources + run intermediates."""
    from utxoproof.evidence import build_manifest_db, produce_evidence_zip, record_source

    data = reports_dir.parent
    imports = data / "imports"
    root = registry.root.absolute()
    # Account folders are the evidence inputs. The root utxoproof.toml can hold
    # RPC credentials and is deliberately not copied into the audit bundle.
    sources = sorted(path for path in (registry.root / "accounts").rglob("*") if path.is_file())
    source_names = {
        str(path.resolve()): path.absolute().relative_to(root).as_posix() for path in sources
    }
    candidates = [
        manual,
        merged_ledgers,
        merged_trades,
        imports / "bank-rows.csv",
    ]
    intermediate = sorted(
        {path for path in candidates if path is not None and Path(path).is_file()}
    )
    attachments: list[Path] = []
    evidence_root = data / "evidence"
    for (filename,) in db.execute(
        "SELECT filename FROM source_manifest WHERE role='attachment' AND filename LIKE ? "
        "ORDER BY filename",
        (f"{year}/%",),
    ).fetchall():
        path = evidence_root / str(filename)
        if path.is_file():
            attachments.append(path)
    arcnames = dict(source_names)
    arcnames.update(
        {
            str(path.resolve()): path.absolute().relative_to(evidence_root.absolute()).as_posix()
            for path in attachments
        }
    )
    manifest_path = reports_dir / str(year) / "evidence-manifest.db"
    manifest = build_manifest_db(manifest_path)
    try:
        for path in sources:
            record_source(manifest, path, "source", name=source_names[str(path.resolve())])
        for path in intermediate:
            record_source(manifest, path, "intermediate")
        for path in attachments:
            record_source(
                manifest,
                path,
                "attachment",
                name=path.absolute().relative_to(evidence_root.absolute()).as_posix(),
            )
    finally:
        manifest.close()
    return produce_evidence_zip(
        year,
        manifest_path,
        report_html,
        sources,
        intermediate,
        reports_dir / str(year),
        attachments=attachments,
        arcnames=arcnames,
    )


def _build_reports(
    registry: Registry,
    db: sqlite3.Connection,
    options: RunOptions,
    reports_dir: Path,
    manual: Path | None,
    merged_ledgers: Path | None,
    merged_trades: Path | None,
    bank_specs: list[tuple[str | Path, str]],
    bank_rows: list[BankRow],
) -> list[str]:
    from utxoproof.cli import load_price_history
    from utxoproof.matching import match_fiat_legs
    from utxoproof.portfolio import (
        load_price_series,
        portfolio_from_db,
        svg_sparkline,
    )
    from utxoproof.price_oracle import bundled_history_path
    from utxoproof.reports import (
        account_site_pages,
        report_years,
        site_nav,
        write_bank_page,
        write_entity_pages,
        write_full_report,
        write_overview_page,
        write_provenance_page,
        write_report,
    )
    from utxoproof.saga import build_saga_document

    lines: list[str] = []
    as_of = _as_of(options, registry)
    price_at = load_price_history(None)
    if price_at is None:  # pragma: no cover - bundled history is always shipped
        raise RuntimeError("bundled BTC/EUR price history is missing")
    latest = _latest_bundled_date()
    if latest is not None and as_of > latest:
        lines.append(
            "note: as-of capped from "
            f"{as_of.isoformat()} to {latest.isoformat()} (offline price history)"
        )
        as_of = latest
    price = options.price or registry.config.reporting.price or price_at(as_of)
    price_note = (
        "explicit/config price"
        if options.price or registry.config.reporting.price
        else "bundled daily close"
    )
    sources = load_saga_sources(
        merged_ledgers,
        merged_trades,
        bank_specs or None,
    )
    report_year = _report_year(options, registry, manual)
    tax_years = sorted(set(report_years(reports_dir)))
    if report_year is not None:
        tax_years = sorted(set(tax_years) | {report_year})
    bank_expected = bool(bank_rows) or (reports_dir / "bank" / "bank.html").is_file()
    full_year = (
        report_year
        if report_year is not None
        and (
            (_selected(options, "report") and manual is not None)
            or (reports_dir / str(report_year) / "fullreport.html").is_file()
        )
        else None
    )
    base_nav_pages = account_site_pages(tax_years, bank=bank_expected, full_report_year=full_year)
    nav_pages = [*options.nav_extra, *base_nav_pages]

    def nav_for(target: Path) -> list[dict[str, object]]:
        return site_nav(nav_pages, reports_dir, target)

    if _selected(options, "report") and manual is not None and report_year is not None:
        year_out = reports_dir / str(report_year)
        target = write_report(
            manual,
            report_year,
            year_out,
            registry.config.taxpayer.communal_surcharge_rate,
            registry.config.classifier,
            price_at=price_at,
            demo_notice=options.demo_notice,
            nav=nav_for(year_out / "report.html"),
        )
        lines.append(f"wrote {target}")
        if not options.no_zip:
            zip_path = _write_evidence(
                registry,
                db,
                reports_dir,
                report_year,
                target,
                manual,
                merged_ledgers,
                merged_trades,
            )
            lines.append(f"wrote {zip_path}")
    if not _selected(options, "portfolio"):
        return lines
    accounts, fiat, utxo_tags = portfolio_config(registry)
    portfolio, advisories = portfolio_from_db(
        db,
        accounts,
        fiat,
        registry.by_kind("wallet")[0].id if registry.by_kind("wallet") else "",
        price_at,
        price,
        as_of,
        utxo_tags,
        labels=output_labels(db),
        address_labels=address_labels(db),
    )
    documents = {}
    for utxo in portfolio.utxos:
        document = build_saga_document(db, utxo.txid, utxo.vout, price_at, sources)
        documents[(utxo.txid, utxo.vout)] = document
        utxo.coverage_share = document.coverage
        utxo.coverage_tier = document.weakest_tier
    sparkline = svg_sparkline(
        load_price_series(bundled_history_path()), label="BTC/EUR daily close, last 12 months"
    )
    overview_target = reports_dir / "overview" / "overview.html"
    overview_links = [
        {"title": str(item["title"]), "href": str(item["href"])}
        for item in site_nav(
            [page for page in base_nav_pages if page[0] != "Overview"],
            reports_dir,
            overview_target,
        )
    ]
    overview = write_overview_page(
        portfolio,
        as_of.isoformat(),
        price_note,
        reports_dir / "overview",
        sparkline,
        demo_notice=options.demo_notice,
        reports=overview_links,
        nav=nav_for(overview_target),
    )
    lines.append(f"wrote {overview}")
    flags_by_utxo = {
        f"{item.txid}:{item.vout}": ",".join(sorted(flag.value for flag in item.flags))
        for item in advisories
    }
    review_year = report_year if report_year is not None else (tax_years[-1] if tax_years else None)
    review_links: dict[str, dict[str, str]] = {}
    entities_dir = reports_dir / "entities"
    for account in portfolio.entities:
        if account.kind in ("bank", "credit_card") and bank_expected:
            review_links[account.id] = {
                "title": "Bank review",
                "href": os.path.relpath(reports_dir / "bank" / "bank.html", entities_dir),
            }
        elif account.kind == "exchange" and review_year is not None:
            review_links[account.id] = {
                "title": f"Tax report {review_year}",
                "href": os.path.relpath(
                    reports_dir / str(review_year) / "report.html", entities_dir
                ),
            }
    for target in write_entity_pages(
        portfolio,
        "../provenance",
        entities_dir,
        flags_by_utxo,
        demo_notice=options.demo_notice,
        review_links=review_links,
        nav=nav_for(entities_dir / "account.html"),
    ):
        lines.append(f"wrote {target}")
    for utxo in portfolio.utxos:
        target = write_provenance_page(
            db,
            utxo.txid,
            utxo.vout,
            price_at,
            price,
            as_of,
            reports_dir / "provenance",
            demo_notice=options.demo_notice,
            nav=nav_for(reports_dir / "provenance" / f"provenance_{utxo.txid}_{utxo.vout}.html"),
            saga_sources=sources,
            saga_document=documents[(utxo.txid, utxo.vout)],
        )
        lines.append(f"wrote {target}")
    if bank_rows:
        bank_txs = sources.transactions if sources is not None else []
        matches, unmatched = match_fiat_legs(bank_txs, bank_rows)
        target = write_bank_page(
            "+".join(sorted({profile for _path, profile in bank_specs})),
            bank_rows,
            matches,
            unmatched,
            reports_dir / "bank",
            demo_notice=options.demo_notice,
            provenance_dir=reports_dir / "provenance",
            nav=nav_for(reports_dir / "bank" / "bank.html"),
        )
        lines.append(f"wrote {target}")
    if _selected(options, "report") and manual is not None and report_year is not None:
        targets = [(u.txid, u.vout) for u in portfolio.utxos[:25]]
        target = write_full_report(
            db=db,
            csv_path=manual,
            year=report_year,
            out_dir=reports_dir / str(report_year),
            price_at=price_at,
            current_price_eur=price,
            price_note=price_note,
            as_of=as_of,
            communal_rate=registry.config.taxpayer.communal_surcharge_rate,
            classifier_cfg=registry.config.classifier,
            provenance_targets=targets,
            portfolio=portfolio,
            price_history_svg=sparkline,
            demo_notice=options.demo_notice,
            nav=nav_for(year_out / "fullreport.html"),
            saga_sources=sources,
        )
        lines.append(f"wrote {target}")
    lines.append(
        f"portfolio: {len(portfolio.utxos)} UTXOs, {portfolio.btc_total:.8f} BTC, "
        f"net worth {portfolio.net_worth_eur:,.2f} EUR"
    )
    return lines


def run(options: RunOptions) -> int:
    """Execute the account workflow. Returns a process exit code."""
    options.only = _validate_only(options.only)
    try:
        registry = load_registry(options.input_dir)
    except AccountError as exc:
        print(f"cannot run: {exc}")
        return 2
    for line in registry_summary(registry):
        print(line)
    if not registry.ok:
        print("cannot run: input errors above")
        return 2
    data = output_data_dir(str(options.data_dir))
    input_root = registry.root.resolve()
    if data.resolve().is_relative_to(input_root):
        print("cannot run: --data-dir must be outside the input tree")
        return 2
    plan = build_run_plan(registry, options)
    if options.plan:
        print("plan:")
        for item in plan:
            mark = "selected" if item["selected"] else "skipped"
            print(f"  [{mark}] {item['step']}: {item['action']}")
        return 0
    try:
        _as_of(options, registry)
    except ValueError as exc:
        print(f"cannot run: {exc}")
        return 2
    if (
        _selected(options, "sync")
        and not options.offline
        and registry.by_kind("wallet")
        and registry.config.chain.backend not in ("electrum", "core")
    ):
        print(
            "cannot run: unknown chain backend "
            f"{registry.config.chain.backend!r}; use electrum or core"
        )
        return 2
    imports = data / "imports"
    reports = data / "reports"
    imports.mkdir(parents=True, exist_ok=True)
    reports.mkdir(parents=True, exist_ok=True)
    db = _open_db(data / "utxoproof.db")
    persist_registry(db, registry)
    # Re-attribute known outputs even for report-only runs; a full sync will
    # apply the strict external-output pass once its graph is complete.
    attribute_outputs(db, registry, strict=False)
    if _selected(options, "labels"):
        for line in _import_labels(registry, db):
            print(line)
    if _selected(options, "sync") and not options.offline:
        for line in _sync_chain(registry, db, options):
            print(line)
    merged_ledgers = merged_trades = manual = None
    exchange_lines: list[str] = []
    if _selected(options, "exchanges"):
        try:
            merged_ledgers, merged_trades, manual = _write_manual(registry, imports)
            if manual is not None:
                exchange_lines.append(f"wrote {manual}")
        except (OSError, ValueError, csv.Error) as exc:
            print(f"exchange import failed: {exc}")
            return 2
    else:
        for key, path in (
            ("ledgers", imports / "ledgers.csv"),
            ("trades", imports / "trades.csv"),
            ("manual", imports / "manual.csv"),
        ):
            if path.is_file():
                if key == "ledgers":
                    merged_ledgers = path
                elif key == "trades":
                    merged_trades = path
                else:
                    manual = path
    bank_rows: list[BankRow] = []
    bank_specs: list[tuple[str | Path, str]] = []
    if _selected(options, "banks"):
        try:
            bank_csv, bank_rows, bank_specs = _write_bank_rows(registry, imports)
            if bank_csv is not None:
                print(f"wrote {bank_csv}")
                print(f"bank rows: {len(bank_rows)}")
        except (OSError, ValueError, csv.Error) as exc:
            print(f"bank import failed: {exc}")
            return 2
    for line in exchange_lines:
        print(line)
    for line in _build_reports(
        registry,
        db,
        options,
        reports,
        manual,
        merged_ledgers,
        merged_trades,
        bank_specs,
        bank_rows,
    ):
        print(line)
    db.close()
    print(f"site: {reports}")
    return 0
