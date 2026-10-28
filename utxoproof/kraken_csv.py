"""Kraken ledgers.csv parser (plan Sec. 10, Sprint 1).

Standalone parser: reads a Kraken ``ledgers.csv`` export and yields normalized
transactions. Shaped like a DaLI plugin's output (In/Out-style records with KYC
metadata) so it can back a real DaLI plugin later; Sprint 1 consumes it directly
because DaLI is not installable here yet (needs a C toolchain).

Expected columns (Kraken export headers, verified):
ledgers ``txid,refid,time,type,subtype,aclass,asset,amount,fee,balance``;
trades ``txid,ordertxid,pair,time,type,ordertype,price,cost,fee,vol,margin,misc,ledgers``.
A trade is a refid group with an XBT leg and a fiat leg; deposits/withdrawals
are single-leg XBT rows. Margin/rollover legs become taxable disposals/costs
(following DaLI's mapping); ``settled`` rows are ignorable; any other unknown
type raises instead of silently dropping money.
"""

from __future__ import annotations

import csv
import datetime
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path

from utxoproof.exchange import ExchangeTx, FiatFlow, to_manual_csv_rows

KYC_STATUS = "kyc"
SOURCE_TYPE = "exchange_purchase"

BTC_ASSETS = {"XBT", "XXBT", "BTC"}
FIAT_ASSETS = {
    "ZEUR": "EUR",
    "EUR": "EUR",
    "ZUSD": "USD",
    "USD": "USD",
    "ZGBP": "GBP",
    "GBP": "GBP",
    "ZCHF": "CHF",
    "CHF": "CHF",
}


KrakenTx = ExchangeTx  # backward-compat alias


def _split_ledgers_refs(value: str) -> list[str]:
    return [part.strip() for part in (value or "").split(",") if part.strip()]


def _trade_ids(trades: list[dict[str, str]]) -> tuple[str, ...]:
    return tuple(t.get("txid", "").strip() for t in trades if t.get("txid", "").strip())


def parse_kraken_trades(path: str | Path) -> dict[str, dict[str, str]]:
    """Parse a Kraken trades.csv export, keyed by trade txid."""
    trades: dict[str, dict[str, str]] = {}
    with open(Path(path), newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            txid = (row.get("txid") or "").strip()
            if txid:
                trades[txid] = row
    return trades


def _trades_for_group(
    rows: list[dict[str, str]], trades: dict[str, dict[str, str]]
) -> list[dict[str, str]]:
    """Trades rows referencing any ledger txid in this refid group."""
    group_txids = {(r.get("txid") or "").strip() for r in rows}
    matched = []
    for trade in trades.values():
        if group_txids & set(_split_ledgers_refs(trade.get("ledgers", ""))):
            matched.append(trade)
    return matched


def detect_margin_activity(txs: list[ExchangeTx]) -> bool:
    """True when any parsed trade used margin (feeds classifier leverage)."""
    return any(tx.margin for tx in txs)


def _parse_time(value: str) -> datetime.date:
    # Kraken exports naive local timestamps; treat as UTC (day-level use only).
    return datetime.datetime.strptime(value.strip(), "%Y-%m-%d %H:%M:%S").date()  # noqa: DTZ007


def parse_kraken_ledgers(
    path: str | Path,
    trades_path: str | Path | None = None,
    skipped: list[tuple[str, str]] | None = None,
) -> list[KrakenTx]:
    """Parse a Kraken ledgers.csv export into normalized BTC transactions.

    With ``trades_path``, each refid group is joined to the trades rows that
    reference its ledger txids: execution price/fee win over leg division and
    margined trades are flagged (``margin`` column non-zero).
    """
    path = Path(path)
    trades = parse_kraken_trades(trades_path) if trades_path else {}
    groups: dict[str, list[dict[str, str]]] = {}
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            refid = (row.get("refid") or "").strip()
            if refid:
                groups.setdefault(refid, []).append(row)

    result: list[KrakenTx] = []
    for refid, rows in groups.items():
        tx = _parse_group(refid, rows, str(path), _trades_for_group(rows, trades), skipped)
        if tx is not None:
            result.append(tx)
    result.sort(key=lambda tx: (tx.date.isoformat(), tx.refid))
    return result


def parse_kraken_fiat_flows(path: str | Path) -> list[FiatFlow]:
    """Parse external EUR deposits/withdrawals from a Kraken ``ledgers.csv``.

    The BTC parser intentionally drops non-BTC refid groups, which also drops
    the euro funding leg. The saga bank layer needs those rows to join a bank
    debit to the exchange deposit that funded a trade. Only ``deposit`` and
    ``withdrawal`` groups are returned; internal transfers and trades are not
    fiat funding.
    """
    path = Path(path)
    groups: dict[str, list[dict[str, str]]] = {}
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            refid = (row.get("refid") or "").strip()
            if refid:
                groups.setdefault(refid, []).append(row)

    flows: list[FiatFlow] = []
    for refid, rows in groups.items():
        entry_type = (rows[0].get("type") or "").strip().lower()
        if entry_type not in ("deposit", "withdrawal"):
            continue
        eur_rows = [
            row for row in rows if FIAT_ASSETS.get((row.get("asset") or "").strip()) == "EUR"
        ]
        if not eur_rows:
            continue
        amount = sum((Decimal(row.get("amount") or "0") for row in eur_rows), Decimal("0"))
        if amount == 0:
            continue
        # A deposit is positive and a withdrawal negative in Kraken's export;
        # a sign that disagrees with the type is corruption, not a transfer.
        if (entry_type == "deposit" and amount < 0) or (entry_type == "withdrawal" and amount > 0):
            raise ValueError(
                f"Kraken refid {refid}: {entry_type} EUR amount has unexpected sign {amount}"
            )
        flows.append(
            FiatFlow(
                date=_parse_time(rows[0].get("time", "")),
                kind=entry_type.upper(),
                eur=abs(amount),
                refid=refid,
                source_evidence=f"{path.name}:refid:{refid}",
            )
        )
    flows.sort(key=lambda flow: (flow.date.isoformat(), flow.refid))
    return flows


def _parse_group(
    refid: str,
    rows: list[dict[str, str]],
    filename: str,
    trades: list[dict[str, str]] | None = None,
    skipped: list[tuple[str, str]] | None = None,
) -> KrakenTx | None:
    """One refid group -> zero or one record (official type table, see module doc)."""
    first = rows[0]
    date = _parse_time(first.get("time", ""))
    evidence = f"{filename}:refid:{refid}"
    entry_type = (first.get("type") or "").strip().lower()
    subtype = (first.get("subtype") or "").strip().lower()
    subclass = (first.get("subclass") or "").strip()
    wallet = (first.get("wallet") or "").strip()

    def skip(reason: str) -> None:
        if skipped is not None:
            skipped.append((refid, reason))

    def base(
        kind: str,
        btc: Decimal,
        price: Decimal,
        fee: Decimal,
        label: str,
        margin: bool = False,
    ) -> KrakenTx:
        return KrakenTx(
            date,
            kind,
            btc,
            price,
            fee,
            refid,
            exchange="kraken",
            source_label=label,
            source_evidence=evidence,
            margin=margin,
            wallet=wallet,
            subclass=subclass,
        )

    if entry_type == "rollover":
        fee_rows = [r for r in rows if (r.get("asset") or "").strip() in FIAT_ASSETS]
        fee_eur = sum((abs(Decimal(r.get("amount") or "0")) for r in fee_rows), Decimal("0"))
        fee_eur += sum((abs(Decimal(r.get("fee") or "0")) for r in rows), Decimal("0"))
        return base(
            "ROLLOVER",
            Decimal("0"),
            Decimal("0"),
            fee_eur,
            f"Kraken rollover financing cost {refid}",
        )
    if entry_type == "settled":
        skip("settled: margin position settled on spot, no movement")
        return None

    if entry_type in ("margin", "margin trade"):
        btc_rows = [r for r in rows if (r.get("asset") or "").strip() in BTC_ASSETS]
        fiat_rows = [r for r in rows if (r.get("asset") or "").strip() in FIAT_ASSETS]
        if not btc_rows and not fiat_rows:
            skip(f"{entry_type}: non-BTC leg, out of scope")
            return None
        amount = sum((Decimal(r.get("amount") or "0") for r in btc_rows), Decimal("0"))
        if amount == 0:
            fee_eur = sum(
                (
                    abs(Decimal(r.get("amount") or "0"))
                    for r in rows
                    if (r.get("asset") or "").strip() in FIAT_ASSETS
                ),
                Decimal("0"),
            )
            fee_eur += sum((abs(Decimal(r.get("fee") or "0")) for r in rows), Decimal("0"))
            return base(
                "MARGIN",
                Decimal("0"),
                Decimal("0"),
                fee_eur,
                f"Kraken margin financing cost {refid}",
                margin=True,
            )
        return _parse_margin_disposal(
            refid, rows, btc_rows, date, evidence, trades or [], wallet, subclass
        )

    if entry_type in ("spend", "receive"):
        return _parse_spend_receive(
            refid, rows, date, evidence, trades or [], wallet, subclass, skip
        )

    if entry_type == "adjustment":
        return _parse_adjustment(refid, rows, date, evidence, wallet, subclass)

    if entry_type == "earn":
        if subtype == "reward" or not subtype:
            btc_rows = [r for r in rows if (r.get("asset") or "").strip() in BTC_ASSETS]
            if not btc_rows:
                skip("earn: non-BTC reward")
                return None
            btc = abs(Decimal(btc_rows[0].get("amount") or "0"))
            return base("DEPOSIT", btc, Decimal("0"), Decimal("0"), f"Kraken earn reward {refid}")
        if subtype in _INTERNAL_SUBTYPES:
            skip(f"earn/{subtype}: internal allocation move")
            return None
        raise ValueError(f"Kraken refid {refid}: unsupported earn subtype {subtype!r}")

    if entry_type == "invite bonus":
        btc_rows = [r for r in rows if (r.get("asset") or "").strip() in BTC_ASSETS]
        if not btc_rows:
            skip("invite bonus: non-BTC reward")
            return None
        return base(
            "DEPOSIT",
            abs(Decimal(btc_rows[0].get("amount") or "0")),
            Decimal("0"),
            Decimal("0"),
            f"Kraken invite bonus {refid}",
        )

    if entry_type == "transfer":
        if subtype in _INTERNAL_SUBTYPES:
            skip(f"transfer/{subtype}: internal move between balances")
            return None
        btc_rows = [r for r in rows if (r.get("asset") or "").strip() in BTC_ASSETS]
        if not btc_rows:
            return None
        return base(
            "DEPOSIT",
            abs(Decimal(btc_rows[0].get("amount") or "0")),
            Decimal("0"),
            Decimal("0"),
            f"Kraken transfer {refid}",
        )

    btc_rows = [r for r in rows if (r.get("asset") or "").strip() in BTC_ASSETS]
    if not btc_rows:
        return None  # non-BTC group (e.g. ETH trade)

    if entry_type == "trade":
        return _parse_trade(refid, rows, btc_rows, date, evidence, trades or [], wallet, subclass)
    if entry_type == "withdrawal":
        return base(
            "WITHDRAWAL",
            abs(Decimal(btc_rows[0].get("amount") or "0")),
            Decimal("0"),
            Decimal("0"),
            f"Kraken withdrawal {refid}",
        )
    if entry_type == "deposit":
        return base(
            "DEPOSIT",
            abs(Decimal(btc_rows[0].get("amount") or "0")),
            Decimal("0"),
            Decimal("0"),
            f"Kraken deposit {refid}",
        )
    if entry_type == "staking":
        return base(
            "DEPOSIT",
            abs(Decimal(btc_rows[0].get("amount") or "0")),
            Decimal("0"),
            Decimal("0"),
            f"Kraken staking reward {refid}",
        )
    raise ValueError(f"Kraken refid {refid}: unsupported ledger type {entry_type!r}")


_INTERNAL_SUBTYPES = frozenset(
    {
        "allocation",
        "deallocation",
        "autoallocate",
        "migration",
        "spottostaking",
        "stakingfromspot",
        "stakingtospot",
        "spotfromstaking",
        "spottofutures",
        "spotfromfutures",
    }
)


def _parse_margin_disposal(
    refid: str,
    rows: list[dict[str, str]],
    btc_rows: list[dict[str, str]],
    date: datetime.date,
    evidence: str,
    trades: list[dict[str, str]],
    wallet: str,
    subclass: str,
) -> KrakenTx | None:
    """Non-zero margin settlement leg -> taxable MARGIN disposal."""
    if not btc_rows:
        return None  # non-BTC margin leg; out of scope like any altcoin row
    btc_amount = sum((Decimal(r.get("amount") or "0") for r in btc_rows), Decimal("0"))
    if btc_amount == 0:
        return None
    price = Decimal("0")
    if trades:
        price = Decimal(trades[0].get("price") or "0")
    if price <= 0:
        fiat_rows = [r for r in rows if (r.get("asset") or "").strip() in FIAT_ASSETS]
        fiat_total = sum((abs(Decimal(r.get("amount") or "0")) for r in fiat_rows), Decimal("0"))
        if fiat_total > 0:
            price = fiat_total / abs(btc_amount)
    if price <= 0:
        raise ValueError(
            f"Kraken refid {refid}: margin disposal of {abs(btc_amount)} BTC without price; "
            "link its closing trade via --trades (trades.csv ledgers column) or check "
            "whether the fiat leg sits in a sibling refid group"
        )
    btc = abs(btc_amount)
    btc_fee = sum((Decimal(r.get("fee") or "0") for r in btc_rows), Decimal("0"))
    fiat_fee = sum(
        (
            Decimal(r.get("fee") or "0")
            for r in rows
            if (r.get("asset") or "").strip() in FIAT_ASSETS
        ),
        Decimal("0"),
    )
    return KrakenTx(
        date,
        "MARGIN",
        btc,
        price,
        abs(fiat_fee) + abs(btc_fee) * price,
        refid,
        exchange="kraken",
        source_label=f"Kraken margin settlement {refid} ({btc} BTC @ {price:.2f})",
        source_evidence=evidence,
        margin=True,
        wallet=wallet,
        subclass=subclass,
        trade_refs=_trade_ids(trades),
    )


def _parse_spend_receive(
    refid: str,
    rows: list[dict[str, str]],
    date: datetime.date,
    evidence: str,
    trades: list[dict[str, str]],
    wallet: str,
    subclass: str,
    skip: Callable[[str], None],
) -> KrakenTx | None:
    """Instant-buy spend/receive pairs -> trade-equivalent; lone legs -> cashflow."""
    spend = [r for r in rows if (r.get("type") or "").strip().lower() == "spend"]
    receive = [r for r in rows if (r.get("type") or "").strip().lower() == "receive"]

    def _btc(rs: list[dict[str, str]]) -> Decimal:
        return sum(
            (
                Decimal(r.get("amount") or "0")
                for r in rs
                if (r.get("asset") or "").strip() in BTC_ASSETS
            ),
            Decimal("0"),
        )

    btc_in, btc_out = _btc(receive), _btc(spend)
    if btc_in == 0 and btc_out == 0:
        return None
    if not spend or not receive:
        # Lone leg without counterpart: cashflow, no gain computed.
        btc = abs(btc_in + btc_out)
        kind = "DEPOSIT" if receive else "WITHDRAWAL"
        return KrakenTx(
            date,
            kind,
            btc,
            Decimal("0"),
            Decimal("0"),
            refid,
            exchange="kraken",
            source_label=f"Kraken lone {'receive' if kind == 'DEPOSIT' else 'spend'} {refid}",
            source_evidence=evidence,
            wallet=wallet,
            subclass=subclass,
        )
    # Paired spend+receive (e.g. instant buy: fiat out, BTC in).
    net = btc_in + btc_out  # spend amounts are negative
    if net == 0:
        skip("spend/receive nets to zero")
        return None
    price = Decimal("0")
    if trades:
        price = Decimal(trades[0].get("price") or "0")
    if price <= 0:
        fiat_rows = [r for r in rows if (r.get("asset") or "").strip() in FIAT_ASSETS]
        fiat_total = sum((abs(Decimal(r.get("amount") or "0")) for r in fiat_rows), Decimal("0"))
        if fiat_total > 0 and abs(net) > 0:
            price = fiat_total / abs(net)
    if price <= 0:
        raise ValueError(f"Kraken refid {refid}: spend/receive without price")
    kind = "BUY" if net > 0 else "SELL"
    return KrakenTx(
        date,
        kind,
        abs(net),
        price,
        Decimal("0"),
        refid,
        exchange="kraken",
        source_label=f"Kraken instant {'buy' if kind == 'BUY' else 'sell'} {refid}",
        source_evidence=evidence,
        wallet=wallet,
        subclass=subclass,
    )


def _parse_adjustment(
    refid: str,
    rows: list[dict[str, str]],
    date: datetime.date,
    evidence: str,
    wallet: str,
    subclass: str,
) -> KrakenTx | None:
    """Delisting conversion (out old asset, in new asset) -> disposal of outflow."""
    btc_out = abs(
        sum(
            (
                Decimal(r.get("amount") or "0")
                for r in rows
                if (r.get("asset") or "").strip() in BTC_ASSETS
                and Decimal(r.get("amount") or "0") < 0
            ),
            Decimal("0"),
        )
    )
    btc_in = sum(
        (
            Decimal(r.get("amount") or "0")
            for r in rows
            if (r.get("asset") or "").strip() in BTC_ASSETS and Decimal(r.get("amount") or "0") > 0
        ),
        Decimal("0"),
    )
    if btc_out == 0:
        if btc_in > 0:
            return KrakenTx(
                date,
                "DEPOSIT",
                btc_in,
                Decimal("0"),
                Decimal("0"),
                refid,
                exchange="kraken",
                source_label=f"Kraken adjustment credit {refid}",
                source_evidence=evidence,
                wallet=wallet,
                subclass=subclass,
            )
        return None  # non-BTC conversion; out of scope
    fiat_rows = [r for r in rows if (r.get("asset") or "").strip() in FIAT_ASSETS]
    fiat_total = sum((abs(Decimal(r.get("amount") or "0")) for r in fiat_rows), Decimal("0"))
    price = fiat_total / btc_out if fiat_total > 0 else Decimal("0")
    if price <= 0:
        raise ValueError(f"Kraken refid {refid}: adjustment without price")
    return KrakenTx(
        date,
        "ADJUSTMENT",
        btc_out,
        price,
        Decimal("0"),
        refid,
        exchange="kraken",
        source_label=f"Kraken delisting conversion {refid}",
        source_evidence=evidence,
        wallet=wallet,
        subclass=subclass,
    )


def _parse_trade(
    refid: str,
    rows: list[dict[str, str]],
    btc_rows: list[dict[str, str]],
    date: datetime.date,
    evidence: str,
    trades: list[dict[str, str]],
    wallet: str = "",
    subclass: str = "",
) -> KrakenTx | None:
    fiat_rows = [r for r in rows if (r.get("asset") or "").strip() in FIAT_ASSETS]
    if not fiat_rows:
        return None
    btc_amount = sum((Decimal(r.get("amount") or "0") for r in btc_rows), Decimal("0"))
    fiat_amount = sum((Decimal(r.get("amount") or "0") for r in fiat_rows), Decimal("0"))
    if btc_amount == 0 or fiat_amount == 0:
        return None
    fiat_ccy = FIAT_ASSETS[fiat_rows[0].get("asset", "").strip()]

    btc = abs(btc_amount)
    margined = any(Decimal(t.get("margin") or "0") != 0 for t in trades)
    kind = "MARGIN" if margined else ("BUY" if btc_amount > 0 else "SELL")
    # Kraken quotes fiat legs in their own currency; Sprint 1 handles EUR legs
    # exactly and converts other currencies at 1:1 only when explicitly paired
    # downstream (full ECB conversion arrives with the fiat-leg matcher).
    fiat_abs = abs(fiat_amount)
    eur_per_btc = fiat_abs / btc

    btc_fee = sum((Decimal(r.get("fee") or "0") for r in btc_rows), Decimal("0"))
    fiat_fee = sum((Decimal(r.get("fee") or "0") for r in fiat_rows), Decimal("0"))
    fee_eur = abs(fiat_fee) + abs(btc_fee) * eur_per_btc
    if trades:
        # Execution economics win over leg division (exact price/cost/fee).
        trade = trades[0]
        trade_price = Decimal(trade.get("price") or "0")
        trade_fee = Decimal(trade.get("fee") or "0")
        if trade_price > 0:
            eur_per_btc = trade_price
            fee_eur = abs(trade_fee) + abs(btc_fee) * eur_per_btc

    side = "buy" if btc_amount > 0 else "sell"
    label = f"Kraken trade {refid} ({side} {btc} BTC @ {eur_per_btc:.2f} {fiat_ccy})"
    if margined:
        label += " [margin]"
    return KrakenTx(
        date,
        kind,
        btc,
        eur_per_btc,
        fee_eur,
        refid,
        exchange="kraken",
        source_label=label,
        source_evidence=evidence,
        margin=margined,
        wallet=wallet,
        subclass=subclass,
        trade_refs=_trade_ids(trades),
    )


__all__ = [
    "ExchangeTx",
    "FiatFlow",
    "KrakenTx",
    "detect_margin_activity",
    "parse_kraken_fiat_flows",
    "parse_kraken_ledgers",
    "parse_kraken_trades",
    "to_manual_csv_rows",
]
