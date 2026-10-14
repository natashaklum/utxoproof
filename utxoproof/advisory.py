"""Per-UTXO advisory engine (plan Sec. 13, Sprint 5).

Analyzes unspent outputs: tax cost if sold, speculation taint, estate and
borrow candidacy, privacy risk. Cost basis here is the acquisition-date close
(a simplification; lot-matched rp2 inventory integration is pending).
Classification input defaults to untainted; the Sprint 7 classifier will feed
`speculation_tainted`.
"""

from __future__ import annotations

import datetime
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum

from utxoproof.belgian_tax import apply_belgian_tax

# Config defaults mirroring plan Sec. 16 [advisory].
BORROW_TAX_THRESHOLD = Decimal("0.02")
ESTATE_MIN_HOLDING_DAYS = 730
ESTATE_MIN_GAIN_EUR = Decimal("1000")
ESTATE_SCORE_THRESHOLD = 5


class AdvisoryFlag(Enum):
    """Action-only recommendations (volatile, price-dependent).

    Descriptive classification lives in tags (see ``utxoproof.tags``):
    behavior bucket, channel, mixed/privacy-risk. Flags never duplicate tags.
    """

    SELL_FRIENDLY = "sell_friendly"
    HOLD_RECOMMENDED = "hold_recommended"
    BORROW_CANDIDATE = "borrow_candidate"
    ESTATE_CANDIDATE = "estate_candidate"
    TAX_LOSS_CANDIDATE = "tax_loss_candidate"


@dataclass
class UTXOAdvisory:
    txid: str
    vout: int
    amount_btc: Decimal
    acquisition_date: datetime.date | None
    holding_days: int
    acquisition_cost_eur: Decimal
    current_value_eur: Decimal
    unrealized_gain_eur: Decimal
    tax_if_sold_eur: Decimal
    effective_tax_rate: Decimal
    kyc_status: str
    kyc_fraction: Decimal
    speculation_tainted: bool = False
    source_type: str = ""
    address: str = ""
    flags: list[AdvisoryFlag] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def estate_score(advisory: UTXOAdvisory) -> int:
    """Estate-planning score per Sec. 13 (higher = better estate candidate)."""
    score = 0
    if advisory.holding_days > 730:
        score += 3
    elif advisory.holding_days > 365:
        score += 1
    if advisory.unrealized_gain_eur > 5000:
        score += 3
    elif advisory.unrealized_gain_eur > 1000:
        score += 1
    if advisory.kyc_status == "kyc":
        score += 2
    if advisory.kyc_status == "non_kyc":
        score += 1  # clean but harder to prove
    if advisory.kyc_status == "mixed":
        score -= 2  # complicates estate transfer
    return score


def is_borrow_candidate(advisory: UTXOAdvisory) -> bool:
    """Tax cost exceeds typical loan drag + clean KYC + seasoned."""
    if advisory.current_value_eur <= 0:
        return False
    return (
        advisory.tax_if_sold_eur / advisory.current_value_eur > BORROW_TAX_THRESHOLD
        and advisory.kyc_status in ("kyc", "non_kyc")
        and advisory.holding_days > 180
    )


def assign_flags(advisory: UTXOAdvisory) -> list[AdvisoryFlag]:
    """Flag assignment per Sec. 13 rules."""
    flags: list[AdvisoryFlag] = []
    value = advisory.current_value_eur
    gain = advisory.unrealized_gain_eur

    if gain < 0:
        flags.append(AdvisoryFlag.TAX_LOSS_CANDIDATE)
    if gain < 0 or (value > 0 and advisory.tax_if_sold_eur / value < Decimal("0.05")):
        flags.append(AdvisoryFlag.SELL_FRIENDLY)
    if value > 0 and gain / value > Decimal("0.30") and advisory.holding_days > 365:
        flags.append(AdvisoryFlag.HOLD_RECOMMENDED)
    if is_borrow_candidate(advisory):
        flags.append(AdvisoryFlag.BORROW_CANDIDATE)
    if estate_score(advisory) >= ESTATE_SCORE_THRESHOLD:
        flags.append(AdvisoryFlag.ESTATE_CANDIDATE)
    return flags


def analyze_utxo(
    txid: str,
    vout: int,
    amount_btc: Decimal,
    acquisition_date: datetime.date | None,
    acquisition_cost_eur: Decimal,
    current_value_eur: Decimal,
    kyc_status: str,
    kyc_fraction: Decimal,
    as_of: datetime.date,
    speculation_tainted: bool = False,
    source_type: str = "",
    address: str = "",
) -> UTXOAdvisory:
    """Build a fully flagged advisory for one UTXO."""
    holding_days = (as_of - acquisition_date).days if acquisition_date else 0
    gain = current_value_eur - acquisition_cost_eur
    tax = apply_belgian_tax(gain)["tax_eur"]
    advisory = UTXOAdvisory(
        txid=txid,
        vout=vout,
        amount_btc=amount_btc,
        acquisition_date=acquisition_date,
        holding_days=holding_days,
        acquisition_cost_eur=acquisition_cost_eur,
        current_value_eur=current_value_eur,
        unrealized_gain_eur=gain,
        tax_if_sold_eur=tax,
        effective_tax_rate=(tax / gain if gain > 0 else Decimal("0")),
        kyc_status=kyc_status,
        kyc_fraction=kyc_fraction,
        speculation_tainted=speculation_tainted,
        source_type=source_type,
        address=address,
    )
    advisory.flags = assign_flags(advisory)
    return advisory


def analyze_wallet(
    db: sqlite3.Connection,
    price_at: Callable[[datetime.date], Decimal],
    current_price_eur: Decimal,
    as_of: datetime.date,
) -> list[UTXOAdvisory]:
    """Analyze every unspent output. ``price_at`` maps acquisition date->EUR/BTC."""
    advisories = []
    rows = db.execute(
        "SELECT o.txid, o.vout, o.value_sat, o.kyc_status, o.kyc_fraction, t.block_time, "
        "o.address, o.source_type FROM tx_outputs o JOIN transactions t ON t.txid=o.txid "
        "WHERE o.spent_by_txid IS NULL AND o.is_mine = 1"
    ).fetchall()
    for txid, vout, value_sat, kyc_status, kyc_fraction, block_time, address, source_type in rows:
        amount_btc = Decimal(value_sat) / Decimal(100_000_000)
        acquired = datetime.date.fromisoformat(block_time[:10])
        cost = amount_btc * price_at(acquired)
        advisories.append(
            analyze_utxo(
                txid,
                vout,
                amount_btc,
                acquired,
                cost,
                amount_btc * current_price_eur,
                kyc_status,
                Decimal(kyc_fraction),
                as_of,
                address=address or "",
                source_type=source_type or "",
            )
        )
    advisories.sort(key=lambda a: a.tax_if_sold_eur, reverse=True)
    return advisories


def portfolio_summary(advisories: list[UTXOAdvisory]) -> dict[str, Decimal]:
    """Liquidate-everything tax cost, estate/borrow candidate values."""
    total_tax = sum((a.tax_if_sold_eur for a in advisories), Decimal("0"))
    estate = sum(
        (a.current_value_eur for a in advisories if AdvisoryFlag.ESTATE_CANDIDATE in a.flags),
        Decimal("0"),
    )
    borrow = sum(
        (a.current_value_eur for a in advisories if AdvisoryFlag.BORROW_CANDIDATE in a.flags),
        Decimal("0"),
    )
    return {"total_tax_eur": total_tax, "estate_value_eur": estate, "borrow_value_eur": borrow}
