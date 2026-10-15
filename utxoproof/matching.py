"""Fiat-leg matching: bank rows <-> exchange trades (plan Sec. 11).

Deterministic, explainable rules — every match carries its reasons, which is
what makes the bank-review page tax evidence rather than vibes. Two passes,
exact before approximate, so a near-miss never steals an exact match:

- direction: BUY pairs with a bank outflow, SELL with an inflow (both passes);
- date: within +/- ``window_days`` (reasons record the actual offset);
- amount, exact pass: absolute EUR equality (Decimal, no tolerance);
- amount, approximate pass: within min(1 EUR, 1% of the trade leg) — bank
  fees and FX rounding cents, labeled with the delta. Multiple approximate
  candidates stay unmatched for manual review (never guessed);
- reference: the trade refid appears in the normalized bank reference or
  description (strong tier on exact matches).

Greedy one-to-one in trade order. Ambiguous and unmatched rows are reported,
never guessed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal

from utxoproof.banks import BankRow
from utxoproof.exchange import ExchangeTx


@dataclass
class FiatMatch:
    tx: ExchangeTx
    bank: BankRow
    reasons: list[str] = field(default_factory=list)
    tier: str = "standard"  # standard | strong | approximate
    chain_txid: str = ""  # set by callers when the trade joins to a chain tx


def _normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _direction_ok(tx: ExchangeTx, bank: BankRow) -> str | None:
    """Direction reason, or None when the signs disagree (no match)."""
    if tx.kind == "BUY" and bank.amount_eur < 0:
        return "direction outflow<->BUY"
    if tx.kind == "SELL" and bank.amount_eur > 0:
        return "direction inflow<->SELL"
    return None


def _reference_hit(tx: ExchangeTx, bank: BankRow) -> str | None:
    """Reference reason, or None. Normalized substring both ways."""
    refid = _normalize(tx.refid)
    if not refid:
        return None
    haystacks = [_normalize(bank.reference or ""), _normalize(bank.description)]
    for hay in haystacks:
        if refid and refid in hay:
            return f"reference {tx.refid}"
    return None


def _in_window(tx: ExchangeTx, bank: BankRow, window_days: int) -> str | None:
    """Date reason, or None outside the window."""
    offset = (bank.date - tx.date).days
    if abs(offset) > window_days:
        return None
    return f"date {offset:+d}d"


def _tolerance(fiat_amount: Decimal) -> Decimal:
    """Approximate-pass amount window: bank fees and FX rounding, capped."""
    return min(Decimal("1.00"), fiat_amount * Decimal("0.01"))


def match_fiat_legs(
    exchange_txs: list[ExchangeTx],
    bank_rows: list[BankRow],
    window_days: int = 2,
) -> tuple[list[FiatMatch], list[BankRow]]:
    """Greedy one-to-one matching, exact pass before approximate. Returns matches + unmatched."""
    matches: list[FiatMatch] = []
    used_bank: set[int] = set()
    trades = [tx for tx in exchange_txs if tx.kind in ("BUY", "SELL")]
    for tx in trades:
        for i, bank in enumerate(bank_rows):
            if i in used_bank:
                continue
            direction = _direction_ok(tx, bank)
            if direction is None:
                continue
            date_reason = _in_window(tx, bank, window_days)
            if date_reason is None:
                continue
            if abs(bank.amount_eur) != tx.fiat_amount:
                continue
            reasons = [
                date_reason,
                f"amount EUR {tx.fiat_amount:,.2f} exact",
                direction,
            ]
            tier = "standard"
            if ref_reason := _reference_hit(tx, bank):
                reasons.append(ref_reason)
                tier = "strong"
            matches.append(FiatMatch(tx, bank, reasons, tier))
            used_bank.add(i)
            break
    for tx in trades:
        if any(m.tx is tx for m in matches):
            continue
        cands = []
        for i, bank in enumerate(bank_rows):
            if i in used_bank:
                continue
            direction = _direction_ok(tx, bank)
            if direction is None:
                continue
            date_reason = _in_window(tx, bank, window_days)
            if date_reason is None:
                continue
            delta = abs(abs(bank.amount_eur) - tx.fiat_amount)
            if delta == 0 or delta > _tolerance(tx.fiat_amount):
                continue
            cands.append((i, bank, direction, date_reason, delta))
        if len(cands) != 1:
            continue  # zero, or ambiguous: stays unmatched for manual review
        i, bank, direction, date_reason, delta = cands[0]
        reasons = [
            date_reason,
            f"amount EUR {tx.fiat_amount:,.2f} ~Δ€{delta:.2f}",
            direction,
        ]
        if ref_reason := _reference_hit(tx, bank):
            reasons.append(ref_reason)
        matches.append(FiatMatch(tx, bank, reasons, "approximate"))
        used_bank.add(i)
    unmatched = [bank for i, bank in enumerate(bank_rows) if i not in used_bank]
    return matches, unmatched
