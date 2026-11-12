"""Bisq CSV exports: trade history and wallet transactions.

Two real Bisq exports are supported (headers verified against the Bisq source
that writes them):

``tradeHistory.csv``
    ``Trade ID,Date/Time,Market,Price,Deviation,Amount in BTC,Amount,Currency,
    Transaction Fee,Trade Fee BTC,Trade Fee BSQ,Buyer Deposit,Seller Deposit,
    Offer type,Status``

``transactions.csv``
    ``Date/Time,Trade ID,Details,Address,Transaction ID,Amount in BTC,Memo,
    Confirmations``

Trades are non-KYC (Bisq is a P2P exchange). Only completed trades enter the
manual CSV. EUR markets use the stated EUR/BTC price; XMR/BTC and ETH/BTC
trades are valued at the BTC/EUR daily close from the bundled history, because
the altcoin leg is evidence and the BTC received/disposed is what enters the
Bitcoin pool. BSQ-denominated trade fees are surfaced but not converted.

The parser stays deterministic and loud: an unknown direction, unparseable
amount or a crypto market with ambiguous bare ``Buy``/``Sell`` raises instead
of silently booking the wrong side.
"""

from __future__ import annotations

import csv
import datetime
from collections.abc import Callable
from decimal import Decimal, InvalidOperation
from pathlib import Path

from utxoproof.exchange import ExchangeTx

EXCHANGE = "bisq"
KYC_STATUS = "non_kyc"
SOURCE_TYPE = "p2p_purchase"

TRADE_MARKER = "Amount in BTC"
TRANSACTION_MARKER = "Transaction ID"

BUY_VALUES = {"buy", "bid", "taker buy", "maker buy"}
SELL_VALUES = {"sell", "ask", "taker sell", "maker sell"}
COMPLETED_VALUES = {"completed", "complete", "done", "finished"}

PriceAt = Callable[[datetime.date], Decimal]


def _pick(row: dict[str, str], names: list[str]) -> str:
    for name in names:
        if name in row and row[name] not in (None, ""):
            return str(row[name]).strip()
    return ""


def _parse_time(value: str) -> datetime.date:
    value = value.strip()
    if not value:
        raise ValueError("empty Bisq timestamp")
    # ISO 8601 with or without timezone / fractional seconds.
    try:
        return datetime.date.fromisoformat(value[:10])
    except ValueError:
        pass
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%d/%m/%Y %H:%M:%S",
        "%d/%m/%Y",
        "%m/%d/%Y",
    ):
        try:
            return datetime.datetime.strptime(value[:19], fmt).date()  # noqa: DTZ007
        except ValueError:
            continue
    raise ValueError(f"unparseable Bisq timestamp: {value!r}")


def _parse_decimal(value: str, what: str) -> Decimal:
    text = (
        (value or "")
        .strip()
        .replace("\u00a0", "")
        .replace(" ", "")
        .replace("BTC", "")
        .replace("EUR", "")
        .replace("BSQ", "")
    )
    if not text:
        return Decimal("0")
    # Bisq writes plain dot decimals. Accept a Belgian comma defensively, but
    # never guess when both separators are present in an ambiguous order.
    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        text = text.replace(",", ".")
    try:
        return Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(f"unparseable Bisq {what}: {value!r}") from exc


def _market_parts(market: str) -> tuple[str, str]:
    """Return ``(base, quote)`` for ``EUR/BTC``, ``XMRBTC`` or ``BTC/EUR``."""
    text = market.strip().upper().replace("-", "/")
    if "/" in text:
        left, right = text.split("/", 1)
        if left == "BTC":
            return right, left
        return left, right
    for quote in ("BTC", "XBT"):
        if text.endswith(quote) and len(text) > len(quote):
            return text[: -len(quote)], quote
        if text.startswith(quote) and len(text) > len(quote):
            return text[len(quote) :], quote
    return text, ""


def _has_btc_market(market: str) -> bool:
    _base, quote = _market_parts(market)
    return quote in ("BTC", "XBT")


def _direction_from_offer(
    offer: str,
    currency: str,
    market: str,
    crypto_direction: str = "base",
) -> tuple[str, str]:
    """Map Bisq's ``Offer type`` label to BUY/SELL of BTC.

    Current exports use labels like ``Buy BTC`` / ``Sell BTC`` (fiat) and
    ``Buy XMR`` / ``Sell XMR`` (crypto). Older exports may carry a bare
    ``Buy``/``Sell``. For a crypto market the bare label is interpreted from
    the altcoin side by default (``Buy`` = buy XMR = sell BTC); set
    ``crypto_direction = "btc"`` in the account manifest when your export uses
    the BTC side instead.
    """
    raw = offer.strip().lower()
    if not raw:
        raise ValueError("missing Offer type/direction")
    if "buy btc" in raw or "buy xbt" in raw:
        return "BUY", "offer type says buy BTC"
    if "sell btc" in raw or "sell xbt" in raw:
        return "SELL", "offer type says sell BTC"
    if raw.startswith("buy "):
        return "SELL", "offer type says buy the altcoin (BTC leaves)"
    if raw.startswith("sell "):
        return "BUY", "offer type says sell the altcoin (BTC arrives)"
    if raw in BUY_VALUES:
        if currency.upper() == "EUR":
            return "BUY", "offer type buy + EUR market"
        if crypto_direction == "btc":
            return "BUY", "offer type buy + crypto_direction=btc"
        return "SELL", "offer type buy of the quote asset (BTC leaves)"
    if raw in SELL_VALUES:
        if currency.upper() == "EUR":
            return "SELL", "offer type sell + EUR market"
        if crypto_direction == "btc":
            return "SELL", "offer type sell + crypto_direction=btc"
        return "BUY", "offer type sell of the quote asset (BTC arrives)"
    raise ValueError(f"unknown Bisq direction {offer!r} (market {market or '?'})")


def _is_completed(status: str) -> bool:
    text = status.strip().lower()
    return not text or text in COMPLETED_VALUES or text.startswith("completed")


def parse_bisq_trade_history(
    path: str | Path,
    btc_eur: PriceAt | None = None,
    crypto_direction: str = "base",
) -> tuple[list[ExchangeTx], list[str]]:
    """Parse ``tradeHistory.csv`` into BTC trades plus non-fatal warnings."""
    if crypto_direction not in ("base", "btc"):
        raise ValueError("crypto_direction must be 'base' or 'btc'")
    path = Path(path)
    txs: list[ExchangeTx] = []
    warnings: list[str] = []
    with open(path, newline="", encoding="utf-8-sig") as handle:
        for index, row in enumerate(csv.DictReader(handle), start=2):
            trade_id = _pick(row, ["Trade ID", "Trade Id", "tradeId", "ID", "id"])
            status = _pick(row, ["Status", "State"])
            if not _is_completed(status):
                label = trade_id or f"row {index}"
                warnings.append(f"{path}: skipped {status or 'unfinished'} trade {label}")
                continue
            market = _pick(row, ["Market", "market", "Pair"])
            currency = _pick(row, ["Currency", "currency"]).upper()
            amount_btc = _parse_decimal(
                _pick(row, ["Amount in BTC", "BTC Amount", "Amount (BTC)"]), "BTC amount"
            )
            if amount_btc == 0:
                continue
            amount_btc = abs(amount_btc)
            quote_amount = _parse_decimal(_pick(row, ["Amount", "Volume"]), "quote amount")
            quote_amount = abs(quote_amount)
            price = _parse_decimal(_pick(row, ["Price"]), "price")
            offer = _pick(row, ["Offer type", "Direction", "Type", "Offer Type"])
            if not market:
                if currency == "EUR":
                    market = "EUR/BTC"
                else:
                    market = f"{currency}/BTC"
            if not _has_btc_market(market):
                raise ValueError(f"{path}: row {index}: market {market!r} does not quote BTC")
            if not currency:
                currency, _quote = _market_parts(market)
                if currency == "BTC":
                    currency = "EUR"
            kind, direction_note = _direction_from_offer(offer, currency, market, crypto_direction)
            trade_date = _parse_time(_pick(row, ["Date/Time", "Date", "Creation Date"]))
            if currency == "EUR":
                if price <= 0:
                    if amount_btc > 0 and quote_amount > 0:
                        price = quote_amount / amount_btc
                    else:
                        raise ValueError(f"{path}: row {index}: missing EUR price")
                eur_per_btc = price
                has_fiat_leg = True
                valuation = f"stated EUR price {price}"
            else:
                if btc_eur is None:
                    raise ValueError(
                        f"{path}: row {index}: crypto market {market} needs a BTC/EUR "
                        "price; pass bundled price history"
                    )
                eur_per_btc = btc_eur(trade_date)
                has_fiat_leg = False
                valuation = f"BTC/EUR close {eur_per_btc} on {trade_date.isoformat()}"
            tx_fee_btc = _parse_decimal(_pick(row, ["Transaction Fee"]), "transaction fee")
            trade_fee_btc = _parse_decimal(_pick(row, ["Trade Fee BTC"]), "trade fee")
            fee_bsq = _parse_decimal(_pick(row, ["Trade Fee BSQ"]), "BSQ trade fee")
            fee_btc = abs(tx_fee_btc) + abs(trade_fee_btc)
            fee_eur = fee_btc * eur_per_btc
            refid = trade_id or f"bisq-{index}"
            notes = [direction_note, valuation]
            if fee_bsq:
                notes.append(f"BSQ fee {fee_bsq} not converted")
            txs.append(
                ExchangeTx(
                    trade_date,
                    kind,
                    amount_btc,
                    eur_per_btc,
                    fee_eur,
                    refid,
                    exchange=EXCHANGE,
                    kyc_status=KYC_STATUS,
                    source_type=SOURCE_TYPE,
                    source_label=f"Bisq {kind} {amount_btc} BTC ({market})",
                    source_evidence=f"{path.name}:row:{index}",
                    trade_refs=(refid,),
                    market=market,
                    quote_currency=currency,
                    quote_amount=quote_amount,
                    has_fiat_leg=has_fiat_leg,
                    status=status or "Completed",
                    details="; ".join(notes),
                )
            )
    txs.sort(key=lambda tx: (tx.date.isoformat(), tx.refid))
    return txs, warnings


def parse_bisq_transactions(path: str | Path) -> tuple[list[ExchangeTx], list[str]]:
    """Parse ``transactions.csv`` into external BTC deposits/withdrawals.

    Internal wallet shuffles and trade-fee rows are ignored; only external
    deposits/withdrawals are kept. ``Transaction ID`` and ``Address`` survive
    on the record so the HTML review can link to chain provenance.
    """
    path = Path(path)
    txs: list[ExchangeTx] = []
    warnings: list[str] = []
    with open(path, newline="", encoding="utf-8-sig") as handle:
        for index, row in enumerate(csv.DictReader(handle), start=2):
            amount = abs(_parse_decimal(_pick(row, ["Amount in BTC", "BTC Amount"]), "BTC amount"))
            if amount == 0:
                continue
            details = _pick(row, ["Details", "Description"])
            address_field = _pick(row, ["Address"])
            address = address_field
            direction_text = " ".join((details, address_field)).lower()
            if "withdraw" in direction_text or direction_text.startswith("sent"):
                kind = "WITHDRAWAL"
            elif "deposit" in direction_text or direction_text.startswith("received"):
                kind = "DEPOSIT"
            elif "sent to" in direction_text:
                kind = "WITHDRAWAL"
            elif "received" in direction_text:
                kind = "DEPOSIT"
            else:
                # Trade deposits, internal transfers and fee rows are not
                # external cashflows; keep them out of the pool.
                continue
            # The Address column is exported as "<direction> <address>".
            if " " in address:
                address = address.split(" ", 1)[1].strip()
            trade_id = _pick(row, ["Trade ID", "Trade Id"])
            txid = _pick(row, ["Transaction ID", "Tx ID", "TxId"])
            tx_date = _parse_time(_pick(row, ["Date/Time", "Date"]))
            refid = f"{trade_id or 'bisq'}-{txid[:12] or index}"
            txs.append(
                ExchangeTx(
                    tx_date,
                    kind,
                    amount,
                    Decimal("0"),
                    Decimal("0"),
                    refid,
                    exchange=EXCHANGE,
                    kyc_status=KYC_STATUS,
                    source_type="transfer",
                    source_label=f"Bisq {kind.lower()} {amount} BTC",
                    source_evidence=f"{path.name}:row:{index}",
                    trade_refs=(trade_id,) if trade_id else (),
                    details=details,
                    txid=txid,
                    address=address,
                )
            )
    txs.sort(key=lambda tx: (tx.date.isoformat(), tx.refid))
    return txs, warnings


def detect_bisq_export(path: str | Path) -> str:
    """Return ``trade_history`` or ``transactions`` from the CSV header."""
    with open(path, newline="", encoding="utf-8-sig") as handle:
        header = next(csv.reader(handle), [])
    names = {name.strip() for name in header}
    if TRANSACTION_MARKER in names:
        return "transactions"
    if TRADE_MARKER in names or "Trade ID" in names:
        return "trade_history"
    raise ValueError(f"{path}: not a Bisq tradeHistory.csv or transactions.csv export")


def parse_bisq_csv(path: str | Path) -> list[ExchangeTx]:
    """Auto-detect and parse either Bisq CSV export (legacy ``import`` path)."""
    kind = detect_bisq_export(path)
    if kind == "transactions":
        txs, _warnings = parse_bisq_transactions(path)
        return txs
    from utxoproof.cli import load_price_history

    btc_eur = load_price_history(None)
    txs, _warnings = parse_bisq_trade_history(path, btc_eur=btc_eur)
    return txs
