"""utxoproof CLI.

Sprint 0: ``compute --input manual.csv --year Y`` (throwaway verification path,
grows into ``report --year`` in Sprint 2).
Sprint 1: ``import --file kraken.csv --type kraken`` converts an exchange export
to manual-CSV rows for ``compute``.
Sprint 2+: ``report``, ``status``, ``setup``/``sync`` (on-chain skeleton),
``privacy`` (KYC), ``advise`` (per-UTXO advisory).
"""

from __future__ import annotations

import argparse
import csv
import datetime
import sqlite3
import sys
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import TypedDict

from utxoproof import __version__
from utxoproof.belgian_tax import COMMUNAL_SURCHARGE_DEFAULT, apply_belgian_tax
from utxoproof.config import Config
from utxoproof.paths import add_data_dir_arg, db_path, evidence_root


class DisposalDetail(TypedDict):
    date: str
    kind: str
    btc: Decimal
    eur_per_btc: Decimal
    proceeds_eur: Decimal
    cost_basis_eur: Decimal
    gain_eur: Decimal


class ComputeResult(TypedDict):
    disposals: list[DisposalDetail]
    gain_loss_eur: Decimal


class YearlyGain(TypedDict):
    year: int
    gain_eur: Decimal
    disposals: int


class Inventory(TypedDict):
    btc: Decimal
    cost_eur: Decimal


class AlltimeResult(TypedDict):
    per_year: list[YearlyGain]
    inventory: Inventory


def _empty_inventory_error(row: dict[str, str]) -> ValueError:
    """Actionable error for disposals with nothing held.

    A MARGIN disposal on empty inventory is usually a short sale (sell first,
    cover later) or an opening leg missing from the import; a plain SELL
    means buys are missing. Short positions need negative-inventory support,
    which the moving-average pool does not have yet.
    """
    date = row.get("date", "?")
    kind = str(row.get("kind") or row.get("side", "?"))
    if kind == "MARGIN":
        hint = (
            "likely a short sale or an opening margin leg outside the import; "
            "import fuller history, or record the opening position"
        )
    else:
        hint = "buys are missing; check date order, deposits and skipped rows"
    return ValueError(f"{kind} of {row.get('btc')} BTC on {date} with empty inventory ({hint})")


PriceAt = Callable[[datetime.date], Decimal]
"""Receipt-date price lookup (oracle, vendored history, or test constant)."""


def _deposit_unit_price(row_date: str, row_price: Decimal, price_at: PriceAt | None) -> Decimal:
    """Cost basis unit price for a DEPOSIT row.

    Stated price wins; otherwise the receipt-date lookup; otherwise an
    actionable error (guessing would poison every later gain).
    """
    if row_price > 0:
        return row_price
    if price_at is not None:
        return price_at(datetime.date.fromisoformat(str(row_date)[:10]))
    raise ValueError(
        f"DEPOSIT of unknown value on {row_date}: supply --price-history "
        "(e.g. data/btc_eur_daily.csv) so receipt-date cost can be valued"
    )


def _apply_withdrawal(
    pool_btc: Decimal, pool_cost: Decimal, btc: Decimal, row: dict[str, str]
) -> tuple[Decimal, Decimal]:
    """Remove a withdrawal from the pool, clamping to what is tracked.

    Withdrawals are moves, not valuations: clamping cannot invent gains.
    Anything beyond the tracked pool belonged to pre-export holdings, which
    are not modeled — warned loudly, never silently dropped from view.
    """
    import sys

    if btc <= pool_btc:
        taken = pool_cost / pool_btc * btc if pool_btc > 0 else Decimal("0")
        return pool_btc - btc, pool_cost - taken
    print(
        f"WARNING: withdrawal of {btc:f} BTC on {row.get('date', '?')} exceeds "
        f"tracked pool ({pool_btc:f}); pre-export holdings are not modeled, "
        "excess ignored",
        file=sys.stderr,
    )
    return Decimal("0"), Decimal("0")


def compute_details(
    csv_path: str | Path,
    year: int,
    price_at: PriceAt | None = None,
    *,
    strict: bool = False,
) -> ComputeResult:
    """Per-disposal moving-average detail for ``year`` plus the yearly total.

    Returns ``{"disposals": [...], "gain_loss_eur": Decimal}`` where each
    disposal has ``date, btc, eur_per_btc, proceeds_eur, cost_basis_eur,
    gain_eur``. Buy fees join the cost pool, sell fees reduce proceeds.
    DEPOSIT rows enter the pool at stated or receipt-date price.
    Opening balance seeds the pool with pre-export holdings so that disposals
    after the first row are funded from tracked inventory. If ``strict`` is True
    an unfunded disposal raises ValueError instead of clamping with warning.
    """
    total_btc = Decimal("0")
    total_cost = Decimal("0")
    realised_gain = Decimal("0")
    disposals: list[DisposalDetail] = []

    with open(csv_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            row_year = int(str(row["date"])[:4])
            side = str(row["side"]).strip().upper()
            btc = Decimal(str(row["btc"]))
            price = Decimal(str(row["eur_per_btc"]))
            fee = Decimal(str(row.get("fee_eur") or "0"))
            if side == "BUY":
                total_btc += btc
                total_cost += btc * price + fee
            elif side == "DEPOSIT":
                unit = _deposit_unit_price(str(row["date"]), price, price_at)
                total_btc += btc
                total_cost += btc * unit + fee
            elif side == "WITHDRAWAL":
                # Non-taxable move out (e.g. to self-custody): basis travels
                # with the coins; pool shrinks proportionally, no gain booked.
                total_btc, total_cost = _apply_withdrawal(total_btc, total_cost, btc, row)
            elif side == "SELL":
                if total_btc <= Decimal("0"):
                    if strict:
                        raise _empty_inventory_error(row)
                    # Lenient: clamp at zero with a loud warning, book zero gain.
                    # Only a genuinely unfunded SELL still raises when --strict is used.
                    import sys

                    print(
                        f"WARNING: sale of {btc:f} BTC on {row.get('date', '?')} has no "
                        f"tracked inventory (pool is {total_btc:f}); gain booked as 0, "
                        "pre-export holdings are not modeled — use --strict to abort",
                        file=sys.stderr,
                    )
                    cost_basis = Decimal("0")
                    proceeds = btc * price - fee
                    gain = Decimal("0")
                else:
                    avg_unit = total_cost / total_btc if total_btc else Decimal("0")
                    cost_basis = avg_unit * btc
                    proceeds = btc * price - fee
                    gain = proceeds - cost_basis
                if row_year == year:
                    realised_gain += gain
                    disposals.append(
                        {
                            "date": str(row["date"]),
                            "kind": str(row.get("kind") or side),
                            "btc": btc,
                            "eur_per_btc": price,
                            "proceeds_eur": proceeds,
                            "cost_basis_eur": cost_basis,
                            "gain_eur": gain,
                        }
                    )
                total_btc -= btc
                total_cost -= cost_basis
            else:
                raise ValueError(f"Unknown side {row['side']!r}")
    return {"disposals": disposals, "gain_loss_eur": realised_gain}


def _day_nets(
    csv_path: str | Path,
) -> dict[str, Decimal]:
    """Cumulative (buys + deposits - sells) through each date, in file order."""
    cumulative = Decimal("0")
    nets: dict[str, Decimal] = {}
    with open(csv_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            side = str(row["side"]).strip().upper()
            btc = Decimal(str(row["btc"]))
            if side in ("BUY", "DEPOSIT"):
                cumulative += btc
            elif side == "SELL":
                cumulative -= btc
            nets[str(row["date"])] = cumulative
    return nets


def _certain_doubles(csv_path: str | Path) -> list[DiagPair]:
    """Disposal pairs sharing a trade ref: one economic event, booked twice."""
    by_ref: dict[str, list[str]] = {}
    with open(csv_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if str(row["side"]).strip().upper() != "SELL":
                continue
            for ref in str(row.get("trade_refs") or "").split(";"):
                ref = ref.strip()
                if ref:
                    by_ref.setdefault(ref, []).append(
                        f"{row['date']} {row.get('kind') or 'SELL'} {row['btc']}"
                    )
    return [
        {"kind": "CERTAIN", "ref": ref, "first": rows[0], "second": rows[1]}
        for ref, rows in sorted(by_ref.items())
        if len(rows) > 1
    ]


def _probable_doubles(rows: list[DiagDisposal]) -> list[DiagPair]:
    """Same-date MARGIN vs SELL of matching size: suggestive, needs eyes."""
    pairs: list[DiagPair] = []
    for first in rows:
        if first["kind"] != "MARGIN" or not first["shortfall_btc"]:
            continue
        for second in rows:
            if second["kind"] == "MARGIN" or second["date"] != first["date"]:
                continue
            bigger = max(first["btc"], second["btc"])
            if bigger > 0 and abs(first["btc"] - second["btc"]) / bigger <= Decimal("0.01"):
                pairs.append(
                    {
                        "kind": "PROBABLE",
                        "ref": first["date"],
                        "first": f"MARGIN {first['btc']}",
                        "second": f"{second['kind']} {second['btc']}",
                    }
                )
                break
    return pairs


def diagnose(csv_path: str | Path, price_at: PriceAt | None = None) -> DiagResult:
    """Walk the file like the pool does, but never crash: every disposal is
    annotated with pool coverage and cumulative inflow by kind. Shortfalls get
    a verdict (ORDER = day nets fine, sequence lost; STRUCTURAL = funding
    genuinely missing), plus certain/probable double-count suspects."""
    pool_btc = Decimal("0")
    pool_cost = Decimal("0")
    buys_btc = Decimal("0")
    deposits_btc = Decimal("0")
    withdrawals_btc = Decimal("0")
    rows: list[DiagDisposal] = []
    shortfalls = 0
    nets = _day_nets(csv_path)
    with open(csv_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            side = str(row["side"]).strip().upper()
            btc = Decimal(str(row["btc"]))
            price = Decimal(str(row["eur_per_btc"]))
            fee = Decimal(str(row.get("fee_eur") or "0"))
            if side == "BUY":
                pool_btc += btc
                pool_cost += btc * price + fee
                buys_btc += btc
            elif side == "DEPOSIT":
                unit = _deposit_unit_price(str(row["date"]), price, price_at)
                pool_btc += btc
                pool_cost += btc * unit + fee
                deposits_btc += btc
            elif side == "WITHDRAWAL":
                pool_btc, pool_cost = _apply_withdrawal(pool_btc, pool_cost, btc, row)
                withdrawals_btc += btc
            elif side == "SELL":
                shortfall = max(Decimal("0"), btc - pool_btc)
                verdict = ""
                if shortfall > 0:
                    shortfalls += 1
                    # Day nets fine but this row starves: intraday order lost.
                    day_net = nets.get(str(row["date"]), Decimal("0"))
                    verdict = "ORDER" if day_net >= 0 else "STRUCTURAL"
                covered = min(btc, pool_btc)
                basis = pool_cost / pool_btc * covered if pool_btc > 0 else Decimal("0")
                pool_btc -= covered
                pool_cost -= basis
                rows.append(
                    {
                        "date": str(row["date"]),
                        "kind": str(row.get("kind") or side),
                        "btc": btc,
                        "pool_btc": pool_btc + covered,
                        "shortfall_btc": shortfall,
                        "buys_btc": buys_btc,
                        "deposits_btc": deposits_btc,
                        "withdrawals_btc": withdrawals_btc,
                        "verdict": verdict,
                    }
                )
    return {
        "disposals": rows,
        "shortfalls": shortfalls,
        "doubles": _certain_doubles(csv_path),
        "probable": _probable_doubles(rows),
    }


def load_price_history(path: str | Path | None) -> PriceAt | None:
    """Receipt-date price lookup seeded from a (date, close) history CSV.

    Offline and deterministic. ``None`` falls back to the history vendored
    with the package, so deposits work out of the box; dates outside its
    coverage still fail loudly instead of valuing at zero.
    """
    from utxoproof.db import open_memory_db
    from utxoproof.price_oracle import EURPriceOracle, bundled_history_path

    source = Path(path).expanduser() if path else bundled_history_path()
    if not source.is_file():
        raise ValueError(f"price history not found: {source} (pass --price-history explicitly)")
    oracle = EURPriceOracle(open_memory_db())
    oracle.load_csv(source, "BTC/EUR", "price-history")
    return oracle.get_btc_eur


def _add_price_history_arg(sub: argparse.ArgumentParser) -> None:
    sub.add_argument(
        "--price-history",
        default=None,
        help="CSV (date,close_eur) for receipt-date deposit valuation",
    )
    sub.add_argument(
        "--opening-btc",
        type=Decimal,
        default=None,
        help="Seed pool with this many BTC at opening (pre-export holdings)",
    )
    sub.add_argument(
        "--opening-price",
        type=Decimal,
        default=None,
        help="Price per BTC for seeding the opening pool (paired with --opening-btc)",
    )
    sub.add_argument(
        "--opening-cost",
        type=Decimal,
        default=None,
        help="Cost basis for seeding the opening pool (overrides opening-price * opening-btc)",
    )


class DiagDisposal(TypedDict):
    date: str
    kind: str
    btc: Decimal
    pool_btc: Decimal
    shortfall_btc: Decimal
    buys_btc: Decimal
    deposits_btc: Decimal
    withdrawals_btc: Decimal
    verdict: str  # "" when covered; else ORDER | STRUCTURAL


class DiagPair(TypedDict):
    kind: str  # CERTAIN (shared trade ref) | PROBABLE (same date+size)
    ref: str
    first: str
    second: str


class DiagResult(TypedDict):
    disposals: list[DiagDisposal]
    shortfalls: int
    doubles: list[DiagPair]
    probable: list[DiagPair]


def print_diagnosis(result: DiagResult) -> int:
    """Human-readable diagnose() output. Returns shortfall count."""
    for row in result["disposals"]:
        if row["shortfall_btc"]:
            flag = (
                f"SHORTFALL {row['shortfall_btc']:f} BTC "
                f"(pool had {row['pool_btc']:f}, "
                f"{row['buys_btc']:f} bought / {row['deposits_btc']:f} deposited / "
                f"{row['withdrawals_btc']:f} withdrawn) "
                f"[{row['verdict']}]"
            )
        else:
            flag = "ok"
        print(f"{row['date']} {row['kind']} {row['btc']:f} BTC -> {flag}")
    for pair in result["doubles"]:
        print(f"DOUBLE-COUNT {pair['ref']}: {pair['first']} ~= {pair['second']}")
    for pair in result["probable"]:
        print(f"SUSPECT {pair['ref']}: {pair['first']} ~= {pair['second']} (confirm manually)")
    print(f"shortfalls: {result['shortfalls']}")
    return result["shortfalls"]


def compute_year(
    csv_path: str | Path,
    year: int,
    price_at: PriceAt | None = None,
    *,
    strict: bool = False,
) -> dict[str, Decimal]:
    """Compute realised gain/loss for ``year`` from a simple manual CSV.

    CSV columns: ``date,side,btc,eur_per_btc,fee_eur`` where ``date`` is
    ``YYYY-MM-DD`` and ``side`` is ``BUY`` or ``SELL``. Moving-average cost
    basis; buy fees join the cost pool, sell fees reduce proceeds.
    """
    return {
        "gain_loss_eur": compute_details(csv_path, year, price_at, strict=strict)["gain_loss_eur"]
    }


def compute_inventory(
    csv_path: str | Path,
    price_at: PriceAt | None = None,
    *,  # keyword-only after this
    strict: bool = False,
) -> Inventory:
    """Whole-file moving-average inventory (BTC + cost basis, no valuation)."""
    total_btc = Decimal("0")
    total_cost = Decimal("0")
    with open(csv_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            side = str(row["side"]).strip().upper()
            btc = Decimal(str(row["btc"]))
            unit = Decimal(str(row["eur_per_btc"]))
            fee = Decimal(str(row.get("fee_eur") or "0"))
            if side == "BUY":
                total_btc += btc
                total_cost += btc * unit + fee
            elif side == "DEPOSIT":
                deposit_unit = _deposit_unit_price(str(row["date"]), unit, price_at)
                total_btc += btc
                total_cost += btc * deposit_unit + fee
            elif side == "WITHDRAWAL":
                total_btc, total_cost = _apply_withdrawal(total_btc, total_cost, btc, row)
            elif side == "SELL":
                if total_btc <= Decimal("0"):
                    # Lenient: clamp at zero with a loud warning, no basis consumed.
                    import sys

                    print(
                        f"WARNING: sale of {btc:f} BTC has no tracked inventory "
                        f"(pool is {total_btc:f}); gain booked as 0, "
                        "pre-export holdings are not modeled — use --strict to abort",
                        file=sys.stderr,
                    )
                    # still consume 0 btc and 0 cost
                    pass
                else:
                    basis = total_cost / total_btc * btc if total_btc else Decimal("0")
                    total_btc -= btc
                    total_cost -= basis
            else:
                raise ValueError(f"Unknown side {row['side']!r}")
    return {"btc": total_btc, "cost_eur": total_cost}


def compute_status(
    csv_path: str | Path, price_eur: Decimal, price_at: PriceAt | None = None
) -> dict[str, Decimal]:
    """Current holdings snapshot at ``price_eur`` (whole-file inventory).

    Returns ``btc, cost_eur, avg_cost_eur, value_eur, unrealized_eur``.
    Same moving-average pool as ``compute_details`` (fees included).
    """
    inventory = compute_inventory(csv_path, price_at)
    total_btc = inventory["btc"]
    total_cost = inventory["cost_eur"]
    value = total_btc * price_eur
    return {
        "btc": total_btc,
        "cost_eur": total_cost,
        "avg_cost_eur": total_cost / total_btc if total_btc else Decimal("0"),
        "value_eur": value,
        "unrealized_eur": value - total_cost,
    }


def compute_alltime(csv_path: str | Path, price_at: PriceAt | None = None) -> AlltimeResult:
    """Per-year gains plus remaining inventory across the whole file."""
    years: set[int] = set()
    with open(csv_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            years.add(int(str(row["date"])[:4]))
    per_year: list[YearlyGain] = []
    for year in sorted(years):
        details = compute_details(csv_path, year, price_at)
        gain = details["gain_loss_eur"]
        per_year.append({"year": year, "gain_eur": gain, "disposals": len(details["disposals"])})
    inventory = compute_inventory(csv_path, price_at)
    return {"per_year": per_year, "inventory": inventory}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="utxoproof", description="Bitcoin wealth + tax tool")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    add_data_dir_arg(parser)
    sub = parser.add_subparsers(dest="command", required=True)
    compute = sub.add_parser("compute", help="Sprint 0 gain/loss + Belgian tax check")
    compute.add_argument("--input", required=True, help="Manual CSV path")
    compute.add_argument("--year", required=True, type=int, help="Tax year, e.g. 2023")
    compute.add_argument(
        "--communal-rate",
        default=COMMUNAL_SURCHARGE_DEFAULT,
        type=Decimal,
        help="Communal surcharge rate (default 0.07)",
    )
    _add_price_history_arg(compute)
    impi = sub.add_parser("import", help="Convert an exchange export to manual CSV")
    impi.add_argument("--file", required=True, help="Source file to import")
    impi.add_argument(
        "--type",
        required=True,
        choices=[
            "kraken",
            "coinbase",
            "binance",
            "bisq",
            "ing",
            "kbc",
            "bnp",
            "belfius",
            "argenta",
        ],
        help="Source type (exchanges -> manual CSV; banks -> bank rows CSV)",
    )
    impi.add_argument(
        "--kyc",
        default="kyc",
        choices=["kyc", "non_kyc", "unknown"],
        help="Override KYC status (default: exchange default)",
    )
    impi.add_argument("--config", default=None, help="utxoproof.toml path")
    impi.add_argument(
        "--out",
        default=None,
        help="Output manual-CSV path (default: stdout)",
    )
    impi.add_argument(
        "--trades",
        default=None,
        help="Kraken trades.csv to join (execution prices, margin flags)",
    )
    report = sub.add_parser("report", help="Generate an HTML tax report")
    report.add_argument("--input", required=True, help="Manual CSV path")
    report.add_argument("--year", type=int, default=None, help="Tax year, e.g. 2023")
    report.add_argument("--alltime", action="store_true", help="All-time summary instead")
    report.add_argument("--out", required=True, help="Output directory")
    report.add_argument("--config", default=None, help="utxoproof.toml path")
    _add_price_history_arg(report)
    report.add_argument("--db", default=None, help="SQLite DB (full report + oracle)")
    report.add_argument("--evidence-dir", default=None, help="Evidence root")
    report.add_argument("--full", action="store_true", help="Compose fullreport.html too")
    report.add_argument(
        "--utxo",
        action="append",
        default=[],
        help="txid:vout for provenance (repeatable; default: all unspent, max 25)",
    )
    report.add_argument(
        "--price", type=Decimal, default=None, help="BTC/EUR price (full report only)"
    )
    report.add_argument("--as-of", default=None, help="As-of date YYYY-MM-DD")
    report.add_argument(
        "--source", action="append", default=[], help="Raw source file (repeatable)"
    )
    report.add_argument("--no-zip", action="store_true", help="Skip evidence ZIP")
    report.add_argument(
        "--communal-rate",
        default=None,
        type=Decimal,
        help="Communal surcharge rate (default: config [taxpayer])",
    )
    report.add_argument("--entities", default=None, help="Entities TOML (overview section)")
    report.add_argument("--wallet", default="utxoproof_watchonly", help="Synced wallet")
    report.add_argument("--ledgers", default=None, help="Kraken ledgers.csv (funding labels)")
    report.add_argument("--trades", default=None, help="Kraken trades.csv join for ledgers")
    setup = sub.add_parser("setup", help="Create watch-only wallet, import xpubs")
    setup.add_argument("--rpc-url", default="http://127.0.0.1:8332")
    setup.add_argument("--rpc-user", default="")
    setup.add_argument("--rpc-password", default="")
    setup.add_argument("--wallet", default="utxoproof_watchonly")
    setup.add_argument("--xpub", required=True, help="Account xpub (xpub/ypub/zpub)")
    setup.add_argument("--fingerprint", required=True, help="Master fingerprint (8 hex)")
    setup.add_argument("--purpose", type=int, default=84)
    setup.add_argument("--coin", type=int, default=0)
    setup.add_argument("--account", type=int, default=0)
    setup.add_argument(
        "--timestamp",
        default="now",
        help="Descriptor import time: 'now' (no rescan) or unix time / 0 to rescan",
    )
    sync = sub.add_parser("sync", help="Pull new on-chain transactions into SQLite")
    sync.add_argument("--rpc-url", default="http://127.0.0.1:8332")
    sync.add_argument("--rpc-user", default="")
    sync.add_argument("--rpc-password", default="")
    sync.add_argument("--wallet", default="utxoproof_watchonly")
    sync.add_argument("--db", default=None, help="SQLite DB (default: <data-dir>/utxoproof.db)")
    status = sub.add_parser("status", help="Holdings, cost basis, unrealized P&L")
    status.add_argument("--input", required=True, help="Manual CSV path")
    status.add_argument("--price", type=Decimal, default=None, help="BTC/EUR price override")
    status.add_argument("--db", default=None, help="SQLite DB (default: <data-dir>/utxoproof.db)")
    status.add_argument("--out", default=None, help="Output directory for status.html")
    _add_price_history_arg(status)
    check = sub.add_parser("check", help="Diagnose pool coverage without crashing")
    check.add_argument("--input", required=True, help="Manual CSV path")
    _add_price_history_arg(check)
    privacy = sub.add_parser("privacy", help="KYC analysis and mixing events")
    privacy.add_argument("--db", default=None, help="SQLite DB (default: <data-dir>/utxoproof.db)")
    privacy.add_argument("--out", default=None, help="Output directory for privacy.html")
    advise = sub.add_parser("advise", help="Per-UTXO advisory table")
    advise.add_argument("--db", default=None, help="SQLite DB (default: <data-dir>/utxoproof.db)")
    advise.add_argument("--price", type=Decimal, default=None, help="BTC/EUR price override")
    advise.add_argument("--as-of", default=None, help="As-of date YYYY-MM-DD (default: today)")
    advise.add_argument("--out", default=None, help="Output directory for advisory.html")
    advise.add_argument(
        "--min-value", type=Decimal, default=Decimal("0"), help="Min EUR value to show"
    )
    prov = sub.add_parser("provenance", help="Chain-of-custody report for a UTXO")
    prov.add_argument("utxo", help="txid:vout")
    prov.add_argument("--db", default=None, help="SQLite DB (default: <data-dir>/utxoproof.db)")
    prov.add_argument("--price", type=Decimal, default=None, help="Current BTC/EUR price")
    prov.add_argument("--as-of", default=None, help="As-of date YYYY-MM-DD (default: today)")
    prov.add_argument("--depth", type=int, default=100, help="Max chain depth")
    prov.add_argument("--out", default=None, help="Output directory")
    prov.add_argument("--evidence-dir", default=None, help="Evidence root")
    prov.add_argument("--ledgers", default=None, help="Kraken ledgers.csv (funding labels)")
    prov.add_argument("--trades", default=None, help="Kraken trades.csv join for ledgers")
    port = sub.add_parser("portfolio", help="Entity overview from synced UTXOs")
    port.add_argument("--db", default=None, help="SQLite DB (<data-dir>/utxoproof.db)")
    port.add_argument("--entities", required=True, help="Entities TOML file")
    port.add_argument("--wallet", default="utxoproof_watchonly", help="Synced wallet")
    port.add_argument("--ledgers", default=None, help="Kraken ledgers.csv (funding labels)")
    port.add_argument("--trades", default=None, help="Kraken trades.csv join for ledgers")
    port.add_argument("--price", type=Decimal, default=None, help="BTC/EUR price override")
    port.add_argument("--as-of", default=None, help="As-of date YYYY-MM-DD (default: today)")
    port.add_argument("--out", default=None, help="Output directory for overview.html + entities/")
    attach = sub.add_parser("attach", help="Register a supporting file (scan, PDF, screenshot)")
    attach.add_argument("--file", required=True, help="File to register (copied in)")
    attach.add_argument("--db", default=None, help="SQLite DB (default: <data-dir>/utxoproof.db)")
    attach.add_argument("--tx", default=None, help="Transaction it supports")
    attach.add_argument("--note", default="", help="What this file proves")
    attach.add_argument("--year", type=int, default=None, help="Evidence year")
    attach.add_argument("--evidence-dir", default=None, help="Evidence root")
    return parser


def _open_db(path: str) -> sqlite3.Connection:
    from utxoproof.db import init_db

    db_path = Path(path).expanduser()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(db_path))
    init_db(db)
    return db


def _write_full_from_args(args: argparse.Namespace, communal: Decimal, config: Config) -> Path:
    """Compose fullreport.html from report args (db + oracle/constant curve)."""
    from utxoproof.reports import write_full_report

    db = _open_db(str(db_path(args)))
    history = load_price_history(args.price_history)
    if history is None:
        raise ValueError("report --full needs --price-history for acquisition pricing")
    price, note = _resolve_price(args.price, args)
    as_of = (
        datetime.date.fromisoformat(args.as_of)
        if args.as_of
        else datetime.datetime.now(datetime.UTC).date()
    )
    targets = _parse_utxo_targets(args, db)
    portfolio = None
    price_history_svg = ""
    if getattr(args, "entities", None):
        from utxoproof.portfolio import (
            load_entities,
            load_price_series,
            portfolio_from_db,
            svg_sparkline,
        )
        from utxoproof.price_oracle import bundled_history_path

        entities, fiat = load_entities(args.entities)
        portfolio, _advisories = portfolio_from_db(
            db, entities, fiat, args.wallet, history, price, as_of
        )
        price_history_svg = svg_sparkline(
            load_price_series(bundled_history_path()),
            label="BTC/EUR daily close, last 12 months",
        )
    funding_matches: dict[str, str] = {}
    if getattr(args, "ledgers", None):
        from utxoproof.linking import funding_from_ledgers

        try:
            funding_matches = funding_from_ledgers(
                args.ledgers, db, trades_path=args.trades
            ).matches
        except ValueError as exc:
            print(f"funding labels skipped: {exc}")
    return write_full_report(
        db=db,
        csv_path=args.input,
        year=args.year,
        out_dir=args.out,
        price_at=history,
        current_price_eur=price,
        price_note=note,
        as_of=as_of,
        communal_rate=communal,
        classifier_cfg=config.classifier,
        provenance_targets=targets,
        portfolio=portfolio,
        price_history_svg=price_history_svg,
        funding=funding_matches,
    )


def _parse_utxo_targets(args: argparse.Namespace, db: sqlite3.Connection) -> list[tuple[str, int]]:
    """Explicit --utxo list, else all unspent (capped)."""
    if args.utxo:
        targets = []
        for item in args.utxo:
            txid, vout = item.rsplit(":", 1)
            targets.append((txid, int(vout)))
        return targets
    rows = db.execute(
        "SELECT txid, vout FROM tx_outputs WHERE spent_by_txid IS NULL LIMIT 26"
    ).fetchall()
    if len(rows) > 25:
        print("note: provenance limited to 25 UTXOs")
    return [(r[0], r[1]) for r in rows[:25]]


def _run_setup(args: argparse.Namespace) -> int:
    from utxoproof.bitcoin_rpc import BitcoinRPC
    from utxoproof.descriptors import build_descriptors
    from utxoproof.onchain import BitcoinCoreOnchainImporter

    rpc = BitcoinRPC(args.rpc_url, args.rpc_user, args.rpc_password)
    importer = BitcoinCoreOnchainImporter(rpc, _open_db(":memory:"))
    importer.setup_wallet(args.wallet)
    descriptors = build_descriptors(
        args.xpub, args.fingerprint, args.purpose, args.coin, args.account
    )
    raw_timestamp = getattr(args, "timestamp", "now")
    timestamp = int(raw_timestamp) if str(raw_timestamp).isdigit() else raw_timestamp
    for kind, desc in descriptors.items():
        importer.import_descriptor(args.wallet, desc, timestamp)
        print(f"imported {kind}: {desc}")
    return 0


def _run_sync(args: argparse.Namespace) -> int:
    from utxoproof.bitcoin_rpc import BitcoinRPC
    from utxoproof.onchain import BitcoinCoreOnchainImporter

    rpc = BitcoinRPC(args.rpc_url, args.rpc_user, args.rpc_password)
    importer = BitcoinCoreOnchainImporter(rpc, _open_db(str(db_path(args))))
    summary = importer.sync(args.wallet)
    print(f"sync: {summary['new_txs']} new / {summary['txs_seen']} seen")
    return 0


def _resolve_price(price: Decimal | None, args: argparse.Namespace) -> tuple[Decimal, str]:
    if price is not None:
        return price, "explicit --price"
    import datetime

    from utxoproof.paths import db_path
    from utxoproof.price_oracle import EURPriceOracle

    db = _open_db(str(db_path(args)))
    day = datetime.datetime.now(datetime.UTC).date() - datetime.timedelta(days=1)
    oracle = EURPriceOracle(db)
    return oracle.get_btc_eur(day), f"Kraken close {day.isoformat()}"


def _run_status(args: argparse.Namespace) -> int:
    from utxoproof.reports import write_status_page

    price, note = _resolve_price(args.price, args)
    history = load_price_history(args.price_history)
    status = compute_status(args.input, price, history)
    print(f"holdings_btc: {status['btc']:.8f}")
    print(f"cost_basis_eur: {status['cost_eur']:.2f}")
    print(f"avg_cost_eur: {status['avg_cost_eur']:.2f}")
    print(f"price_eur: {price:.2f} ({note})")
    print(f"value_eur: {status['value_eur']:.2f}")
    print(f"unrealized_eur: {status['unrealized_eur']:.2f}")
    if args.out:
        target = write_status_page(args.input, price, note, args.out, price_at=history)
        print(f"wrote {target}")
    return 0


def _run_advise(args: argparse.Namespace) -> int:
    from utxoproof.advisory import analyze_wallet, portfolio_summary
    from utxoproof.kyc import propagate_graph, seed_source_kyc
    from utxoproof.reports import write_advisory_page

    db = _open_db(str(db_path(args)))
    seed_source_kyc(db)
    propagate_graph(db)
    as_of = (
        datetime.date.fromisoformat(args.as_of)
        if args.as_of
        else datetime.datetime.now(datetime.UTC).date()
    )
    price: Decimal
    curve: Callable[[datetime.date], Decimal]
    if args.price is not None:
        price = Decimal(args.price)
        note = "explicit --price"

        def curve(_day: datetime.date) -> Decimal:
            return price

    else:
        from utxoproof.price_oracle import EURPriceOracle

        oracle = EURPriceOracle(db)
        curve = oracle.get_btc_eur
        note = "daily close per acquisition date"
        price = oracle.get_btc_eur(as_of)
    advisories = [
        a for a in analyze_wallet(db, curve, price, as_of) if a.current_value_eur >= args.min_value
    ]
    for a in advisories:
        flags = ",".join(sorted(f.value for f in a.flags))
        print(f"{a.txid}:{a.vout} {a.amount_btc:.8f}BTC tax={a.tax_if_sold_eur:.2f} [{flags}]")
    summary = portfolio_summary(advisories)
    print(f"liquidate_tax_eur: {summary['total_tax_eur']:.2f}")
    if args.out:
        target = write_advisory_page(advisories, price, note, as_of.isoformat(), args.out)
        print(f"wrote {target}")
    return 0


def _run_privacy(args: argparse.Namespace) -> int:
    from utxoproof.kyc import detect_mixing_events, kyc_summary, propagate_graph, seed_source_kyc
    from utxoproof.reports import write_privacy_page

    db = _open_db(str(db_path(args)))
    seed_source_kyc(db)
    propagate_graph(db)
    summary = kyc_summary(db)
    print("kyc_summary: " + ", ".join(f"{k}={v}" for k, v in summary.items()))
    events = detect_mixing_events(db)
    print(f"mixing_events: {len(events)}")
    if args.out:
        target = write_privacy_page(db, args.out)
        print(f"wrote {target}")
    return 0


def _run_portfolio(args: argparse.Namespace) -> int:
    from utxoproof.linking import FundingResult, funding_from_ledgers
    from utxoproof.portfolio import (
        load_entities,
        load_price_series,
        portfolio_from_db,
        svg_sparkline,
    )
    from utxoproof.price_oracle import bundled_history_path
    from utxoproof.reports import (
        write_entity_pages,
        write_overview_page,
        write_provenance_page,
    )

    db = _open_db(str(db_path(args)))
    entities, fiat = load_entities(args.entities)
    as_of = (
        datetime.date.fromisoformat(args.as_of)
        if args.as_of
        else datetime.datetime.now(datetime.UTC).date()
    )
    price: Decimal
    curve: Callable[[datetime.date], Decimal]
    if args.price is not None:
        price = Decimal(args.price)
        note = "explicit --price"

        def curve(_day: datetime.date) -> Decimal:
            return price

    else:
        from utxoproof.price_oracle import EURPriceOracle

        oracle = EURPriceOracle(db)
        curve = oracle.get_btc_eur
        note = "daily close per acquisition date"
        price = oracle.get_btc_eur(as_of)
    funding = FundingResult(matches={})
    try:
        funding = funding_from_ledgers(args.ledgers, db, trades_path=args.trades)
    except ValueError as exc:
        print(f"funding labels skipped: {exc}")
    for line in funding.ambiguous:
        print(f"ambiguous funding: {line} — left unlabeled")
    for line in funding.unmatched:
        print(f"unmatched withdrawal: {line} — no chain receive in window")
    portfolio, advisories = portfolio_from_db(db, entities, fiat, args.wallet, curve, price, as_of)
    print(f"utxos: {len(portfolio.utxos)} btc={portfolio.btc_total:.8f}")
    for eid, label, value, share in portfolio.allocation():
        print(f"entity {eid} ({label}): {value:,.2f} EUR ({share:.1f}%)")
    print(f"net_worth_eur: {portfolio.net_worth_eur:,.2f}")
    if args.out:
        history = load_price_series(bundled_history_path())
        sparkline = svg_sparkline(history, label="BTC/EUR daily close, last 12 months")
        overview_target = write_overview_page(
            portfolio, as_of.isoformat(), note, Path(args.out) / "overview", sparkline
        )
        print(f"wrote {overview_target}")
        flags_by_utxo = {
            f"{a.txid}:{a.vout}": ",".join(sorted(f.value for f in a.flags)) for a in advisories
        }
        for target in write_entity_pages(
            portfolio, "../provenance", Path(args.out) / "entities", flags_by_utxo
        ):
            print(f"wrote {target}")
        prov_dir = Path(args.out) / "provenance"
        for utxo in portfolio.utxos:
            target = write_provenance_page(
                db,
                utxo.txid,
                utxo.vout,
                curve,
                price,
                as_of,
                prov_dir,
                funding=funding.matches,
            )
            print(f"wrote {target}")
    return 0


def _run_provenance(args: argparse.Namespace) -> int:
    from utxoproof.provenance import build_provenance_chain
    from utxoproof.reports import write_provenance_page

    try:
        txid, vout_str = args.utxo.rsplit(":", 1)
        vout = int(vout_str)
    except ValueError:
        raise ValueError(f"UTXO must look like txid:vout, got {args.utxo!r}") from None
    db = _open_db(str(db_path(args)))
    as_of = (
        datetime.date.fromisoformat(args.as_of)
        if args.as_of
        else datetime.datetime.now(datetime.UTC).date()
    )
    price: Decimal
    curve: Callable[[datetime.date], Decimal]
    if args.price is not None:
        price = Decimal(args.price)
        note = "explicit --price"

        def curve(_day: datetime.date) -> Decimal:
            return price

    else:
        from utxoproof.price_oracle import EURPriceOracle

        oracle = EURPriceOracle(db)
        curve = oracle.get_btc_eur
        note = "daily close per step date"
        price = oracle.get_btc_eur(as_of)
    steps = build_provenance_chain(txid, vout, db, curve, args.depth)
    print(
        f"chain: {len(steps)} steps back to {steps[0].txid}:{steps[0].vout}" if steps else "empty"
    )
    if steps and len(steps) >= args.depth:
        print(f"note: chain truncated at depth cap {args.depth} — earliest history not shown")
    print(f"price_note: {note}")
    if args.out:
        from utxoproof.linking import FundingResult, funding_from_ledgers

        funding = FundingResult(matches={})
        try:
            funding = funding_from_ledgers(args.ledgers, db, trades_path=args.trades)
        except ValueError as exc:
            print(f"funding labels skipped: {exc}")
        target = write_provenance_page(
            db,
            txid,
            vout,
            curve,
            price,
            as_of,
            args.out,
            args.depth,
            evidence_root=evidence_root(args),
            funding=funding.matches,
        )
        print(f"wrote {target}")
    return 0


def _run_attach(args: argparse.Namespace) -> int:
    import datetime

    from utxoproof.evidence import attach_file

    db = _open_db(str(db_path(args)))
    year = args.year or datetime.datetime.now(datetime.UTC).date().year
    entry = attach_file(db, args.file, evidence_root(args), year, args.tx, args.note)
    print(f"registered {entry['filename']} sha256={entry['sha256'][:16]}…")
    if entry["txid"]:
        print(f"linked to {entry['txid']}")
    return 0


def _run_import(args: argparse.Namespace) -> int:
    from utxoproof.banks import BANK_PROFILES, parse_bank_csv
    from utxoproof.binance_csv import parse_binance_csv
    from utxoproof.bisq_csv import parse_bisq_csv
    from utxoproof.coinbase_csv import parse_coinbase_csv
    from utxoproof.exchange import to_manual_csv_rows
    from utxoproof.kraken_csv import parse_kraken_ledgers

    if args.type in ("kraken", "coinbase", "binance", "bisq"):
        parser = {
            "coinbase": parse_coinbase_csv,
            "binance": parse_binance_csv,
            "bisq": parse_bisq_csv,
        }.get(args.type)
        if parser is not None:
            if args.trades:
                raise ValueError("--trades only applies to --type kraken")
            txs = parser(args.file)
        else:
            from utxoproof.config import find_config, load_config
            from utxoproof.kraken_csv import detect_margin_activity, parse_kraken_ledgers

            txs = parse_kraken_ledgers(args.file, args.trades)
            if detect_margin_activity(txs):
                config = load_config(args.config)
                if not config.classifier.used_leverage:
                    source = find_config(args.config)
                    where = f" ({source})" if source else " (no file found; see --config)"
                    print(
                        "note: margin trading detected but used_leverage is not set "
                        f"in utxoproof.toml [classifier]{where}",
                        file=sys.stderr,
                    )
        if args.kyc != "kyc":
            for tx in txs:
                tx.kyc_status = args.kyc
        rows = to_manual_csv_rows(txs)
    elif args.type in BANK_PROFILES:
        bank_rows = parse_bank_csv(args.file, args.type)
        rows = [
            {
                "date": r.date.isoformat(),
                "side": "",
                "btc": "",
                "eur_per_btc": "",
                "fee_eur": "",
                "description": r.description,
                "amount_eur": str(r.amount_eur),
            }
            for r in bank_rows
        ]
    else:  # pragma: no cover - argparse choices guard this
        raise ValueError(f"Unsupported type {args.type!r}")

    fieldnames = list(rows[0].keys()) if rows else ["date"]
    if args.out:
        with open(args.out, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
    else:
        writer = csv.DictWriter(sys.stdout, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    print(
        f"imported {len(rows)} transactions ({args.type})",
        file=sys.stderr,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "compute":
        result = compute_year(args.input, args.year, load_price_history(args.price_history))
        gain = result["gain_loss_eur"]
        tax = apply_belgian_tax(gain, "goede_huisvader", args.communal_rate)
        print(f"year: {args.year}")
        print(f"gain_loss_eur: {gain:.2f}")
        print(f"tax_eur: {tax['tax_eur']:.2f}")
        print(f"communal_surcharge_eur: {tax['communal_surcharge_eur']:.2f}")
        print(f"total_eur: {tax['total_eur']:.2f}")
        return 0
    if args.command == "check":
        diagnosis = diagnose(args.input, load_price_history(args.price_history))
        print_diagnosis(diagnosis)
        return 0
    if args.command == "import":
        return _run_import(args)
    if args.command == "report":
        from utxoproof.config import load_config
        from utxoproof.evidence import build_manifest_db, produce_evidence_zip, record_source
        from utxoproof.reports import write_alltime_page, write_report

        config = load_config(args.config)
        communal = (
            Decimal(args.communal_rate)
            if args.communal_rate is not None
            else config.taxpayer.communal_surcharge_rate
        )
        history = load_price_history(args.price_history)
        if args.alltime:
            target = write_alltime_page(args.input, args.out, price_at=history)
            print(f"wrote {target}")
            return 0
        if args.year is None:
            raise ValueError("report needs --year Y or --alltime")
        target = write_report(
            args.input,
            args.year,
            args.out,
            communal,
            config.classifier,
            price_at=load_price_history(args.price_history),
        )
        print(f"wrote {target}")
        if args.full:
            full = _write_full_from_args(args, communal, config)
            print(f"wrote {full}")
        if not args.no_zip:
            manifest_db_path = Path(args.out) / "evidence-manifest.db"
            manifest_db = build_manifest_db(manifest_db_path)
            for source in [args.input, *args.source]:
                record_source(manifest_db, source, "source")
            year_dir = evidence_root(args) / str(args.year)
            attached = sorted(year_dir.glob("*")) if year_dir.is_dir() else []
            for file in attached:
                if file.is_file():
                    record_source(manifest_db, file, "attachment", name=f"{args.year}/{file.name}")
            manifest_db.close()
            zip_path = produce_evidence_zip(
                args.year,
                manifest_db_path,
                target,
                [Path(s) for s in args.source],
                [Path(args.input)],
                args.out,
                attachments=attached,
            )
            print(f"wrote {zip_path}")
        return 0
    if args.command == "setup":
        return _run_setup(args)
    if args.command == "sync":
        return _run_sync(args)
    if args.command == "status":
        return _run_status(args)
    if args.command == "privacy":
        return _run_privacy(args)
    if args.command == "advise":
        return _run_advise(args)
    if args.command == "provenance":
        return _run_provenance(args)
    if args.command == "portfolio":
        return _run_portfolio(args)
    if args.command == "attach":
        return _run_attach(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())
