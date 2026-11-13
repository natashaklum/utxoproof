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
from dataclasses import dataclass, field
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
from utxoproof.banks import BankRow, load_bank_rows_csv, parse_bank_csv
from utxoproof.bisq_receipts import BisqReceipt
from utxoproof.config import Config
from utxoproof.db import init_db
from utxoproof.exchange import ExchangeTx
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


@dataclass
class ExchangeImport:
    """Parsed exchange sources for the current run (all adapters)."""

    transactions: list[ExchangeTx] = field(default_factory=list)
    receipts: list[BisqReceipt] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    manual: Path | None = None
    merged_ledgers: Path | None = None
    merged_trades: Path | None = None

    def account_transactions(self, account_id: str) -> list[ExchangeTx]:
        return [tx for tx in self.transactions if tx.account_id == account_id]


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
                "transactions": [str(path) for path in account.transactions],
                "receipts": [str(path) for path in account.receipts],
                "events": [str(path) for path in account.events],
                "wallet_accounts": list(account.wallet_accounts),
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
            "checks": {"labels": 0, "bank_rows": 0, "exchange_txs": 0, "receipts": 0},
            "online": None,
            "ok": False,
        }
    errors = list(registry.errors)
    warnings = list(registry.warnings)
    checks: dict[str, Any] = {
        "labels": 0,
        "bank_rows": 0,
        "exchange_txs": 0,
        "receipts": 0,
    }
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
            try:
                if account.exchange == "kraken":
                    from utxoproof.kraken_csv import parse_kraken_ledgers

                    for ledger in account.ledgers:
                        checks["exchange_txs"] += len(
                            parse_kraken_ledgers(
                                ledger, account.trades[0] if account.trades else None
                            )
                        )
                elif account.exchange == "bisq":
                    from utxoproof.bisq_csv import (
                        parse_bisq_trade_history,
                        parse_bisq_transactions,
                    )
                    from utxoproof.bisq_receipts import parse_bisq_receipt
                    from utxoproof.cli import load_price_history

                    price_at = load_price_history(None)
                    for trade_file in account.trades:
                        txs, trade_warnings = parse_bisq_trade_history(
                            trade_file,
                            btc_eur=price_at,
                            crypto_direction=account.crypto_direction,
                        )
                        checks["exchange_txs"] += len(txs)
                        warnings.extend(trade_warnings)
                    for transfer_file in account.transactions:
                        txs, transfer_warnings = parse_bisq_transactions(transfer_file)
                        checks["exchange_txs"] += len(txs)
                        warnings.extend(transfer_warnings)
                    for receipt_file in account.receipts:
                        try:
                            parse_bisq_receipt(receipt_file)
                            checks["receipts"] += 1
                        except ValueError as exc:
                            warnings.append(str(exc))
                else:
                    from utxoproof.manual_exchange import parse_manual_events

                    for event_file in account.events:
                        checks["exchange_txs"] += len(parse_manual_events(event_file, account.path))
            except (ValueError, OSError, csv.Error) as exc:
                errors.append(f"{account.id}: exchange parse: {exc}")
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
        f"{checks['exchange_txs']} exchange txs, {checks.get('receipts', 0)} receipts"
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


def _write_exchange_csvs(result: ExchangeImport, out_dir: Path) -> list[Path]:
    """Write the deterministic per-exchange review tables used as evidence."""
    out_dir.mkdir(parents=True, exist_ok=True)
    files: list[Path] = []

    trades = out_dir / "exchange-trades.csv"
    with open(trades, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "account_id",
                "date",
                "kind",
                "market",
                "btc",
                "quote_currency",
                "quote_amount",
                "eur_per_btc",
                "fee_eur",
                "refid",
                "status",
                "has_fiat_leg",
                "source",
                "details",
            ],
        )
        writer.writeheader()
        for tx in result.transactions:
            if tx.kind not in ("BUY", "SELL"):
                continue
            writer.writerow(
                {
                    "account_id": tx.account_id,
                    "date": tx.date.isoformat(),
                    "kind": tx.kind,
                    "market": tx.market,
                    "btc": str(tx.btc),
                    "quote_currency": tx.quote_currency,
                    "quote_amount": str(tx.quote_amount),
                    "eur_per_btc": str(tx.eur_per_btc),
                    "fee_eur": str(tx.fee_eur),
                    "refid": tx.refid,
                    "status": tx.status,
                    "has_fiat_leg": "1" if tx.has_fiat_leg else "0",
                    "source": tx.source_evidence,
                    "details": tx.details,
                }
            )
    files.append(trades)

    transfers = out_dir / "exchange-transfers.csv"
    with open(transfers, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "account_id",
                "date",
                "kind",
                "btc",
                "txid",
                "address",
                "refid",
                "details",
                "source",
                "evidence",
            ],
        )
        writer.writeheader()
        for tx in result.transactions:
            if tx.kind not in ("DEPOSIT", "WITHDRAWAL"):
                continue
            writer.writerow(
                {
                    "account_id": tx.account_id,
                    "date": tx.date.isoformat(),
                    "kind": tx.kind,
                    "btc": str(tx.btc),
                    "txid": tx.txid,
                    "address": tx.address,
                    "refid": tx.refid,
                    "details": tx.details,
                    "source": tx.source_evidence,
                    "evidence": ";".join(tx.evidence),
                }
            )
    files.append(transfers)

    receipts = out_dir / "exchange-receipts.csv"
    with open(receipts, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "account_id",
                "filename",
                "date",
                "counterparty_iban",
                "bic",
                "name",
                "currency",
                "btc",
                "price_eur",
                "market",
                "reference",
                "own_iban",
                "sha256",
            ],
        )
        writer.writeheader()
        for receipt in result.receipts:
            writer.writerow(
                {
                    "account_id": receipt.account_id,
                    "filename": receipt.filename,
                    "date": receipt.date.isoformat(),
                    "counterparty_iban": receipt.iban,
                    "bic": receipt.bic,
                    "name": receipt.name,
                    "currency": receipt.currency,
                    "btc": str(receipt.btc),
                    "price_eur": str(receipt.price_eur),
                    "market": receipt.market,
                    "reference": receipt.reference,
                    "own_iban": receipt.own_iban,
                    "sha256": receipt.sha256,
                }
            )
    files.append(receipts)
    return files


def _write_manual(registry: Registry, out_dir: Path, price_at: Any) -> ExchangeImport:
    """Parse every exchange adapter and write one manual CSV for compute/report."""
    from utxoproof.bisq_csv import parse_bisq_trade_history, parse_bisq_transactions
    from utxoproof.bisq_receipts import parse_bisq_receipt
    from utxoproof.evidence import sha256_file
    from utxoproof.exchange import to_manual_csv_rows
    from utxoproof.kraken_csv import parse_kraken_ledgers
    from utxoproof.manual_exchange import parse_manual_events

    exchanges = registry.by_kind("exchange")
    result = ExchangeImport()
    if not exchanges:
        return result
    ledgers = [
        path for account in exchanges if account.exchange == "kraken" for path in account.ledgers
    ]
    trades = [
        path for account in exchanges if account.exchange == "kraken" for path in account.trades
    ]
    result.merged_ledgers = _merge_csv(ledgers, out_dir / "ledgers.csv", unique_column="refid")
    result.merged_trades = _merge_csv(trades, out_dir / "trades.csv")
    for account in exchanges:
        if account.exchange == "kraken":
            for ledger in account.ledgers:
                account_txs = parse_kraken_ledgers(ledger, result.merged_trades)
                for tx in account_txs:
                    tx.account_id = account.id
                result.transactions.extend(account_txs)
        elif account.exchange == "bisq":
            for trade_file in account.trades:
                account_txs, trade_warnings = parse_bisq_trade_history(
                    trade_file,
                    btc_eur=price_at,
                    crypto_direction=account.crypto_direction,
                )
                for tx in account_txs:
                    tx.account_id = account.id
                result.transactions.extend(account_txs)
                result.warnings.extend(trade_warnings)
            for transfer_file in account.transactions:
                account_txs, transfer_warnings = parse_bisq_transactions(transfer_file)
                for tx in account_txs:
                    tx.account_id = account.id
                result.transactions.extend(account_txs)
                result.warnings.extend(transfer_warnings)
            for receipt_file in account.receipts:
                try:
                    receipt = parse_bisq_receipt(receipt_file)
                except ValueError as exc:
                    result.warnings.append(str(exc))
                    continue
                receipt.account_id = account.id
                receipt.sha256 = sha256_file(receipt_file)
                result.receipts.append(receipt)
        else:
            for event_file in account.events:
                account_txs = parse_manual_events(event_file, account.path)
                for tx in account_txs:
                    tx.account_id = account.id
                result.transactions.extend(account_txs)
    result.transactions.sort(key=lambda tx: (tx.date.isoformat(), tx.refid, tx.account_id))
    result.receipts.sort(key=lambda receipt: (receipt.date.isoformat(), receipt.filename))
    rows = to_manual_csv_rows(result.transactions, include_account=True)
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
    out_dir.mkdir(parents=True, exist_ok=True)
    with open(manual, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    result.manual = manual
    _write_exchange_csvs(result, out_dir)
    return result


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
        "counterparty_iban",
        "reference",
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
                    "counterparty_iban": row.counterparty_iban or "",
                    "reference": row.reference or "",
                    "account_id": row.account_id,
                    "source_file": row.source_file,
                    "row_number": row.row_number,
                }
            )
    return target, rows, specs


SAT_PER_BTC = Decimal("100000000")


def _exchange_wallet_context(
    db: sqlite3.Connection,
    registry: Registry,
    exchange_account: Account,
    price: Decimal,
    reports_dir: Path,
    exchange_dir: Path,
) -> tuple[list[dict[str, object]], dict[str, tuple[str, ...]]]:
    """Balance and txid attribution for an exchange's linked wallet accounts."""
    wallet_ids = list(exchange_account.wallet_accounts)
    if not wallet_ids:
        return [], {}
    placeholders = ",".join("?" for _ in wallet_ids)
    balances = {
        str(account_id): (int(count), int(sat))
        for account_id, count, sat in db.execute(
            "SELECT account_id, COUNT(*), COALESCE(SUM(value_sat), 0) FROM tx_outputs "  # noqa: S608 - placeholders only
            f"WHERE spent_by_txid IS NULL AND is_mine = 1 AND account_id IN ({placeholders}) "
            "GROUP BY account_id",
            wallet_ids,
        ).fetchall()
    }
    txids: dict[str, set[str]] = {}
    for txid, account_id in db.execute(
        "SELECT txid, account_id FROM tx_outputs "  # noqa: S608 - placeholders only
        f"WHERE account_id IN ({placeholders}) AND is_mine = 1 "
        "UNION "
        "SELECT spent_by_txid AS txid, account_id FROM tx_outputs "
        f"WHERE spent_by_txid IS NOT NULL AND account_id IN ({placeholders}) "
        "AND is_mine = 1",
        [*wallet_ids, *wallet_ids],
    ).fetchall():
        if txid and account_id:
            txids.setdefault(str(txid), set()).add(str(account_id))

    wallets: list[dict[str, object]] = []
    for wallet_id in wallet_ids:
        wallet = registry.get(wallet_id)
        count, sat = balances.get(wallet_id, (0, 0))
        amount = Decimal(sat) / SAT_PER_BTC
        wallets.append(
            {
                "id": wallet.id,
                "label": wallet.label,
                "network": wallet.network,
                "utxos": count,
                "gap_limit": wallet.gap_limit,
                "btc": f"{amount:.8f}",
                "eur": f"{amount * price:,.2f}",
                "href": os.path.relpath(
                    reports_dir / "entities" / f"{wallet.id}.html", exchange_dir
                ),
            }
        )
    return wallets, {txid: tuple(sorted(owners)) for txid, owners in txids.items()}


def _write_exchange_links(
    result: ExchangeImport,
    bank_matches: list[Any],
    receipt_matches: list[Any],
    out_dir: Path,
) -> Path:
    """Write one row per trade linking trade, bank row, receipt and transfer."""
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / "exchange-links.csv"
    bank_by_tx = {id(match.tx): match for match in bank_matches}
    receipt_by_trade = {
        id(match.trade): match for match in receipt_matches if match.trade is not None
    }
    transfers = [tx for tx in result.transactions if tx.kind in ("DEPOSIT", "WITHDRAWAL")]
    fields = [
        "account_id",
        "trade_ref",
        "trade_date",
        "trade_kind",
        "market",
        "btc",
        "eur_value",
        "quote_currency",
        "quote_amount",
        "has_fiat_leg",
        "status",
        "bank_date",
        "bank_amount_eur",
        "bank_account",
        "bank_source",
        "bank_tier",
        "bank_reasons",
        "receipt_file",
        "receipt_tier",
        "receipt_reasons",
        "transfer_date",
        "transfer_kind",
        "transfer_btc",
        "txid",
        "address",
    ]
    with open(target, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for trade in result.transactions:
            if trade.kind not in ("BUY", "SELL"):
                continue
            bank = bank_by_tx.get(id(trade))
            receipt = receipt_by_trade.get(id(trade))
            refs = set(trade.trade_refs)
            transfer = next((tx for tx in transfers if refs & set(tx.trade_refs)), None)
            writer.writerow(
                {
                    "account_id": trade.account_id,
                    "trade_ref": trade.refid,
                    "trade_date": trade.date.isoformat(),
                    "trade_kind": trade.kind,
                    "market": trade.market,
                    "btc": str(trade.btc),
                    "eur_value": str(trade.fiat_amount),
                    "quote_currency": trade.quote_currency,
                    "quote_amount": str(trade.quote_amount),
                    "has_fiat_leg": "1" if trade.has_fiat_leg else "0",
                    "status": trade.status,
                    "bank_date": bank.bank.date.isoformat() if bank else "",
                    "bank_amount_eur": str(bank.bank.amount_eur) if bank else "",
                    "bank_account": bank.bank.account_id if bank else "",
                    "bank_source": bank.bank.source_file if bank else "",
                    "bank_tier": bank.tier if bank else "",
                    "bank_reasons": "; ".join(bank.reasons) if bank else "",
                    "receipt_file": receipt.receipt.filename if receipt else "",
                    "receipt_tier": receipt.tier if receipt else "",
                    "receipt_reasons": ("; ".join(receipt.reasons or []) if receipt else ""),
                    "transfer_date": transfer.date.isoformat() if transfer else "",
                    "transfer_kind": transfer.kind if transfer else "",
                    "transfer_btc": str(transfer.btc) if transfer else "",
                    "txid": transfer.txid if transfer else "",
                    "address": transfer.address if transfer else "",
                }
            )
    return target


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


def _is_toml_warning_log(path: Path) -> bool:
    """Warning logs sit beside manifests but are not tax evidence."""
    return path.name.startswith("WARNINGS_") and path.name.endswith(".log")


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
    sources = sorted(
        path
        for path in (registry.root / "accounts").rglob("*")
        if path.is_file() and not _is_toml_warning_log(path)
    )
    source_names = {
        str(path.resolve()): path.absolute().relative_to(root).as_posix() for path in sources
    }
    candidates = [
        manual,
        merged_ledgers,
        merged_trades,
        imports / "bank-rows.csv",
        imports / "exchange-trades.csv",
        imports / "exchange-transfers.csv",
        imports / "exchange-receipts.csv",
        imports / "exchange-links.csv",
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
    exchange_import: ExchangeImport,
) -> list[str]:
    from utxoproof.bisq_receipts import match_receipts
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
        entity_review_links,
        report_years,
        site_nav,
        write_bank_page,
        write_entity_pages,
        write_exchange_pages,
        write_full_report,
        write_overview_page,
        write_provenance_page,
        write_report,
    )
    from utxoproof.saga import SagaSources, build_saga_document

    lines: list[str] = []
    as_of = _as_of(options, registry)
    price_at = load_price_history(None)
    if price_at is None:  # pragma: no cover - bundled price history is always shipped
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
        None if bank_rows else (bank_specs or None),
    )
    if sources is None and (exchange_import.transactions or bank_rows):
        sources = SagaSources()
    if sources is not None:
        # Parsed once by the exchange adapters: keep the same objects so the
        # bank/receipt matchers and the saga agree on refids and metadata.
        if exchange_import.transactions:
            sources.transactions = exchange_import.transactions
        if bank_rows:
            sources.bank_rows = bank_rows
    bank_matches, bank_unmatched = (
        match_fiat_legs(exchange_import.transactions, bank_rows) if bank_rows else ([], [])
    )
    receipt_matches, receipt_warnings = (
        match_receipts(exchange_import.receipts, exchange_import.transactions, bank_matches)
        if exchange_import.receipts
        else ([], [])
    )
    if exchange_import.transactions:
        links_csv = _write_exchange_links(
            exchange_import,
            bank_matches,
            receipt_matches,
            reports_dir.parent / "imports",
        )
        lines.append(f"wrote {links_csv}")
    lines.extend(f"WARNING: {warning}" for warning in receipt_warnings)
    report_year = _report_year(options, registry, manual)
    tax_years = sorted(set(report_years(reports_dir)))
    if report_year is not None:
        tax_years = sorted(set(tax_years) | {report_year})
    bank_expected = bool(bank_rows) or (reports_dir / "bank" / "bank.html").is_file()
    exchange_expected = bool(exchange_import.transactions or exchange_import.receipts)
    full_year = (
        report_year
        if report_year is not None
        and (
            (_selected(options, "report") and manual is not None)
            or (reports_dir / str(report_year) / "fullreport.html").is_file()
        )
        else None
    )
    base_nav_pages = account_site_pages(
        tax_years,
        bank=bank_expected,
        exchange=exchange_expected,
        full_report_year=full_year,
    )
    nav_pages = [*options.nav_extra, *base_nav_pages]

    def nav_for(target: Path) -> list[dict[str, object]]:
        return site_nav(nav_pages, reports_dir, target)

    exchange_alerts: list[dict[str, str]] = []
    if exchange_expected:
        exchange_dir = reports_dir / "exchange"
        tax_href = (
            os.path.relpath(reports_dir / str(report_year) / "report.html", exchange_dir)
            if report_year is not None
            else ""
        )
        exchange_pages: list[dict[str, object]] = []
        for account in registry.by_kind("exchange"):
            wallets, wallet_txids = _exchange_wallet_context(
                db, registry, account, price, reports_dir, exchange_dir
            )
            exchange_pages.append(
                {
                    "id": account.id,
                    "label": account.label,
                    "exchange": account.exchange,
                    "path": str(account.path),
                    "wallets": wallets,
                    "wallet_txids": wallet_txids,
                }
            )
        for target in write_exchange_pages(
            exchange_pages,
            exchange_import.transactions,
            exchange_import.receipts,
            bank_matches,
            receipt_matches,
            exchange_dir,
            demo_notice=options.demo_notice,
            provenance_dir=reports_dir / "provenance",
            bank_href=os.path.relpath(reports_dir / "bank" / "bank.html", exchange_dir),
            tax_href=tax_href,
            warnings=[*exchange_import.warnings, *receipt_warnings],
            nav=nav_for(exchange_dir / "index.html"),
        ):
            lines.append(f"wrote {target}")
        fiat_trades = [
            tx
            for tx in exchange_import.transactions
            if tx.kind in ("BUY", "SELL") and tx.has_fiat_leg
        ]
        matched_refs = {match.tx.refid for match in bank_matches}
        unmatched_trades = [tx for tx in fiat_trades if tx.refid not in matched_refs]
        unmatched_receipts = [
            match for match in receipt_matches if match.tier in ("unmatched", "ambiguous")
        ]
        if unmatched_trades or unmatched_receipts or exchange_import.warnings:
            parts = []
            if unmatched_trades:
                parts.append(f"{len(unmatched_trades)} trade(s) without a bank row")
            if unmatched_receipts:
                parts.append(f"{len(unmatched_receipts)} receipt(s) without a trade")
            if exchange_import.warnings:
                parts.append(f"{len(exchange_import.warnings)} import warning(s)")
            exchange_alerts.append(
                {
                    "text": "Exchange review: " + ", ".join(parts) + ".",
                    "href": os.path.relpath(exchange_dir / "index.html", reports_dir / "overview"),
                    "link": "Open exchange review",
                }
            )
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
    overview_alerts = list(exchange_alerts)
    if bank_unmatched:
        overview_alerts.append(
            {
                "text": (
                    f"Bank review: {len(bank_unmatched)} statement row(s) without a matching trade."
                ),
                "href": os.path.relpath(reports_dir / "bank" / "bank.html", overview_target.parent),
                "link": "Open bank review",
            }
        )
    overview = write_overview_page(
        portfolio,
        as_of.isoformat(),
        price_note,
        reports_dir / "overview",
        sparkline,
        demo_notice=options.demo_notice,
        reports=overview_links,
        nav=nav_for(overview_target),
        alerts=overview_alerts,
    )
    lines.append(f"wrote {overview}")
    flags_by_utxo = {
        f"{item.txid}:{item.vout}": ",".join(sorted(flag.value for flag in item.flags))
        for item in advisories
    }
    review_year = report_year if report_year is not None else (tax_years[-1] if tax_years else None)
    entities_dir = reports_dir / "entities"
    review_links = entity_review_links(portfolio, reports_dir, entities_dir, review_year)
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
    if bank_rows and bank_specs:
        target = write_bank_page(
            "+".join(sorted({profile for _path, profile in bank_specs})),
            bank_rows,
            bank_matches,
            bank_unmatched,
            reports_dir / "bank",
            demo_notice=options.demo_notice,
            provenance_dir=reports_dir / "provenance",
            nav=nav_for(reports_dir / "bank" / "bank.html"),
            receipt_matches=receipt_matches,
            warnings=[*exchange_import.warnings, *receipt_warnings],
            exchange_href=(
                os.path.relpath(
                    reports_dir / "exchange" / "index.html",
                    reports_dir / "bank",
                )
                if exchange_expected
                else ""
            ),
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
    from utxoproof.cli import load_price_history

    exchange_import = ExchangeImport()
    exchange_lines: list[str] = []
    if registry.by_kind("exchange"):
        try:
            price_at = load_price_history(None)
            if price_at is None:  # pragma: no cover - bundled history is shipped
                raise RuntimeError("bundled BTC/EUR price history is missing")
            exchange_import = _write_manual(registry, imports, price_at)
            exchange_lines.extend(f"WARNING: {w}" for w in exchange_import.warnings)
            if _selected(options, "exchanges") and exchange_import.manual is not None:
                exchange_lines.append(f"wrote {exchange_import.manual}")
        except (OSError, ValueError, csv.Error, RuntimeError) as exc:
            print(f"exchange import failed: {exc}")
            return 2
    merged_ledgers = exchange_import.merged_ledgers
    merged_trades = exchange_import.merged_trades
    manual = exchange_import.manual
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
    else:
        persisted = imports / "bank-rows.csv"
        if persisted.is_file():
            try:
                bank_rows = load_bank_rows_csv(persisted)
            except (OSError, ValueError, csv.Error) as exc:
                print(f"bank reuse failed: {exc}")
                return 2
            bank_specs = [
                (statement, account.profile)
                for account in [*registry.by_kind("bank"), *registry.by_kind("credit_card")]
                for statement in account.statements
            ]
            if bank_rows:
                print(f"bank rows: {len(bank_rows)} (reused {persisted})")
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
        exchange_import,
    ):
        print(line)
    db.close()
    print(f"site: {reports}")
    return 0
