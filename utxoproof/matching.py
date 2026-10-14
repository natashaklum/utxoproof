"""Fiat-leg matching: bank rows <-> exchange trades (plan Sec. 11).

Deterministic, explainable rules — every match carries its reasons, which is
what makes the bank-review page tax evidence rather than vibes:

- direction: BUY pairs with a bank outflow, SELL with an inflow;
- amount: exact absolute EUR equality (Decimal, no tolerance);
- date: within +/- ``window_days`` ( Reasons record the actual offset);
- reference: the trade refid appears in the normalized bank reference or
  description (strong tier); without it the match is standard tier.

Greedy one-to-one in trade order. Ambiguous and unmatched rows are reported,
never guessed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from utxoproof.banks import BankRow
from utxoproof.exchange import ExchangeTx


@dataclass
class FiatMatch:
    tx: ExchangeTx
    bank: BankRow
    reasons: list[str] = field(default_factory=list)
    tier: str = "standard"  # standard | strong
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


def match_fiat_legs(
    exchange_txs: list[ExchangeTx],
    bank_rows: list[BankRow],
    window_days: int = 2,
) -> tuple[list[FiatMatch], list[BankRow]]:
    """Greedy one-to-one matching. Returns (matches, unmatched_bank_rows)."""
    matches: list[FiatMatch] = []
    used_bank: set[int] = set()
    for tx in exchange_txs:
        if tx.kind not in ("BUY", "SELL"):
            continue
        for i, bank in enumerate(bank_rows):
            if i in used_bank:
                continue
            direction = _direction_ok(tx, bank)
            if direction is None:
                continue
            if abs((bank.date - tx.date).days) > window_days:
                continue
            if abs(bank.amount_eur) != tx.fiat_amount:
                continue
            reasons = [
                f"date {(bank.date - tx.date).days:+d}d",
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
    unmatched = [bank for i, bank in enumerate(bank_rows) if i not in used_bank]
    return matches, unmatched
