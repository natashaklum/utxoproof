"""Bisq SEPA receipt filenames: parse metadata and link receipts to trades.

Bisq's bank-transfer export is a PDF named with the transfer metadata, e.g.::

    20260226_ES7200495NNNNNNNNNNN_BSCHESXXXXX_FIRSTNAME_LASTNAME_1234_EUR_0.01234_BTC_123456.78_EURBTC_export_BE18414...rest.pdf

The PDF itself remains evidence. This module reads the deterministic filename
(date, counterparties, BTC amount, EUR/BTC price, market, optional trade
reference), then matches it to a EUR Bisq trade and the bank row that trade was
already matched to. No PDF text extraction is attempted.

Matching is conservative: date within a small window, BTC amount and price
within a rounding tolerance. Ambiguous and unmatched receipts are returned as
warnings; they are never assigned to a guessed trade.
"""

from __future__ import annotations

import datetime
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

from utxoproof.exchange import ExchangeTx
from utxoproof.matching import FiatMatch

IBAN_RE = re.compile(r"^[A-Z]{2}\d{2}[A-Z0-9]{10,30}$")
BIC_RE = re.compile(r"^[A-Z]{6}[A-Z0-9]{2}([A-Z0-9]{3})?$")
MARKET_RE = re.compile(r"^[A-Z]{2,8}BTC$|^BTC[A-Z]{2,8}$")
REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,}$")


@dataclass
class BisqReceipt:
    path: Path
    filename: str
    date: datetime.date
    iban: str = ""  # counterparty
    bic: str = ""
    name: str = ""
    currency: str = "EUR"
    btc: Decimal = Decimal("0")
    price_eur: Decimal = Decimal("0")
    market: str = ""
    reference: str = ""
    own_iban: str = ""
    account_id: str = ""
    sha256: str = ""

    @property
    def eur_amount(self) -> Decimal:
        return self.btc * self.price_eur

    @property
    def sha256_short(self) -> str:
        return f"{self.sha256[:16]}…" if self.sha256 else "—"


@dataclass
class ReceiptMatch:
    receipt: BisqReceipt
    trade: ExchangeTx | None = None
    bank: object | None = None
    reasons: list[str] | None = None
    tier: str = "unmatched"  # exact | approximate | ambiguous | unmatched


def _decimal(value: str, what: str) -> Decimal:
    text = (value or "").strip().replace(",", ".")
    try:
        return Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(f"unparseable receipt {what}: {value!r}") from exc


def parse_bisq_receipt(path: str | Path) -> BisqReceipt:
    """Parse one Bisq SEPA PDF filename. The file contents are not read."""
    file = Path(path)
    tokens = file.stem.split("_")
    if len(tokens) < 8:
        raise ValueError(f"not a Bisq SEPA receipt filename: {file.name}")
    if not (tokens[0].isdigit() and len(tokens[0]) == 8):
        raise ValueError(f"receipt filename does not start with YYYYMMDD: {file.name}")
    try:
        day = datetime.datetime.strptime(tokens[0], "%Y%m%d").date()  # noqa: DTZ007
    except ValueError as exc:
        raise ValueError(f"receipt filename has an invalid date: {file.name}") from exc

    currency_index = next(
        (i for i, token in enumerate(tokens) if token.upper() in ("EUR", "USD", "GBP", "CHF")),
        None,
    )
    if currency_index is None or currency_index + 4 >= len(tokens):
        raise ValueError(f"receipt filename is missing currency/BTC/price fields: {file.name}")
    currency = tokens[currency_index].upper()
    btc = abs(_decimal(tokens[currency_index + 1], "BTC amount"))
    if tokens[currency_index + 2].upper() != "BTC":
        raise ValueError(f"receipt filename is missing the BTC marker: {file.name}")
    price = abs(_decimal(tokens[currency_index + 3], "EUR/BTC price"))
    market = tokens[currency_index + 4].upper().replace("/", "")
    if not MARKET_RE.match(market):
        raise ValueError(f"receipt filename has no recognisable market: {file.name}")

    ibans = [token for token in tokens if IBAN_RE.match(token)]
    bics = [token for token in tokens if BIC_RE.match(token) and token not in ibans]
    reference = ""
    if currency_index > 0:
        candidate = tokens[currency_index - 1]
        if REF_RE.match(candidate) and candidate.upper() not in ("BTC", "EUR"):
            reference = candidate
    name_tokens = []
    if bics:
        start = tokens.index(bics[0]) + 1
        for token in tokens[start:currency_index]:
            if token == reference:
                continue
            name_tokens.append(token)
    own_iban = ""
    for index, token in enumerate(tokens):
        if token.lower() == "export":
            for later in tokens[index + 1 :]:
                if IBAN_RE.match(later):
                    own_iban = later
                    break
            break
    return BisqReceipt(
        path=file,
        filename=file.name,
        date=day,
        iban=ibans[0] if ibans else "",
        bic=bics[0] if bics else "",
        name=" ".join(name_tokens),
        currency=currency,
        btc=btc,
        price_eur=price,
        market=market,
        reference=reference,
        own_iban=own_iban,
    )


def _market_key(value: str) -> str:
    return value.upper().replace("/", "").replace("-", "")


def _within(actual: Decimal, expected: Decimal, exact: Decimal, relative: Decimal) -> str | None:
    delta = abs(actual - expected)
    if delta <= exact:
        return "exact"
    if delta <= max(exact, expected * relative):
        return "approximate"
    return None


def match_receipts(
    receipts: list[BisqReceipt],
    trades: list[ExchangeTx],
    bank_matches: list[FiatMatch] | None = None,
    window_days: int = 4,
) -> tuple[list[ReceiptMatch], list[str]]:
    """Link receipts to EUR trades and the bank rows those trades matched."""
    bank_by_trade = {id(match.tx): match.bank for match in bank_matches or []}
    eur_trades = [
        tx
        for tx in trades
        if tx.kind in ("BUY", "SELL") and tx.has_fiat_leg and tx.quote_currency == "EUR"
    ]
    matches: list[ReceiptMatch] = []
    warnings: list[str] = []
    for receipt in receipts:
        candidates: list[tuple[ExchangeTx, str, list[str]]] = []
        for trade in eur_trades:
            if receipt.account_id and trade.account_id and receipt.account_id != trade.account_id:
                continue
            offset = (trade.date - receipt.date).days
            if abs(offset) > window_days:
                continue
            if _market_key(receipt.market) != _market_key(trade.market):
                continue
            btc_tier = _within(receipt.btc, trade.btc, Decimal("0.00000001"), Decimal("0.005"))
            price_tier = _within(
                receipt.price_eur, trade.eur_per_btc, Decimal("0.01"), Decimal("0.005")
            )
            if btc_tier is None or price_tier is None:
                continue
            reasons = [
                f"date {offset:+d}d",
                (
                    f"BTC {receipt.btc} exact"
                    if btc_tier == "exact"
                    else f"BTC {receipt.btc} ~Δ{abs(receipt.btc - trade.btc)}"
                ),
                (
                    f"price {receipt.price_eur} exact"
                    if price_tier == "exact"
                    else f"price {receipt.price_eur} ~Δ{abs(receipt.price_eur - trade.eur_per_btc)}"
                ),
            ]
            tier = "exact" if btc_tier == price_tier == "exact" else "approximate"
            candidates.append((trade, tier, reasons))
        if not candidates:
            matches.append(ReceiptMatch(receipt, tier="unmatched", reasons=["no trade match"]))
            warnings.append(
                f"receipt {receipt.filename}: no matching Bisq trade "
                f"(date/BTC/price within ±{window_days}d)"
            )
            continue
        exact = [candidate for candidate in candidates if candidate[1] == "exact"]
        chosen_pool = exact if exact else candidates
        if len(chosen_pool) > 1:
            refs = ", ".join(trade.refid for trade, _tier, _reasons in chosen_pool[:4])
            matches.append(
                ReceiptMatch(
                    receipt,
                    tier="ambiguous",
                    reasons=[f"{len(chosen_pool)} candidate trades: {refs}"],
                )
            )
            warnings.append(
                f"receipt {receipt.filename}: {len(chosen_pool)} candidate trades "
                f"({refs}); left unmatched for manual review"
            )
            continue
        trade, tier, reasons = chosen_pool[0]
        bank = bank_by_trade.get(id(trade))
        if bank is None:
            warnings.append(
                f"receipt {receipt.filename}: matched trade {trade.refid} "
                "but that trade has no matched bank row"
            )
        matches.append(ReceiptMatch(receipt, trade, bank, reasons, tier))
    return matches, warnings
