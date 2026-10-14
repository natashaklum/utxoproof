"""Exchange to on-chain funding matcher (plan v5 Sec. 10).

Kraken ledgers carry no on-chain txid, so the Kraken<->chain join is amount +
date over ours-only outputs: a withdrawal of S BTC on day D funded the chain
transaction that received exactly S, and a deposit of S matches the external
spend that sent exactly S, each within +/- ``window_days``. Exact
single-candidate matches annotate provenance steps; ambiguous or unmatched
legs are reported, never guessed.
"""

from __future__ import annotations

import datetime
import sqlite3
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

SAT_PER_BTC = 100_000_000


@dataclass
class Withdrawal:
    refid: str
    date: datetime.date
    sats: int
    direction: str = "out"  # out = exchange->wallet (withdrawal); in = wallet->exchange (deposit)


@dataclass
class FundingResult:
    """Outcome of matching Kraken legs to chain receives/spends."""

    matches: dict[str, str]  # chain txid -> funding label for provenance steps
    ambiguous: list[str] = field(default_factory=list)  # refids, human-readable
    unmatched: list[str] = field(default_factory=list)  # refids, human-readable


def kraken_btc_withdrawals(
    ledgers_path: str | Path, trades_path: str | Path | None = None
) -> list[Withdrawal]:
    """BTC exchange<->wallet legs from a Kraken ``ledgers.csv`` export.

    ``trades_path`` is the same join ``import --trades`` takes: without it,
    margin-disposal legs that need execution prices raise instead of parsing.
    """
    from utxoproof.kraken_csv import parse_kraken_ledgers

    out = []
    for tx in parse_kraken_ledgers(Path(ledgers_path), trades_path):
        if tx.btc <= 0:
            continue
        if tx.kind == "WITHDRAWAL":
            out.append(
                Withdrawal(
                    refid=tx.refid,
                    date=tx.date,
                    sats=int((tx.btc * SAT_PER_BTC).to_integral_value()),
                    direction="out",
                )
            )
        elif tx.kind == "DEPOSIT":
            out.append(
                Withdrawal(
                    refid=tx.refid,
                    date=tx.date,
                    sats=int((tx.btc * SAT_PER_BTC).to_integral_value()),
                    direction="in",
                )
            )
    return out


def chain_receives(
    db: sqlite3.Connection, mine: bool | None = None
) -> list[tuple[str, datetime.date, int]]:
    """On-chain outputs as ``(creating txid, tx date, value sats)``.

    ``mine`` restricts to ours-only (withdrawal receipts) or external-only
    (deposit sends); ``None`` returns everything (legacy callers, tests).
    """
    if mine is None:
        query = (
            "SELECT o.txid, t.block_time, o.value_sat "
            "FROM tx_outputs o JOIN transactions t ON t.txid = o.txid"
        )
        params: tuple[object, ...] = ()
    else:
        query = (
            "SELECT o.txid, t.block_time, o.value_sat "
            "FROM tx_outputs o JOIN transactions t ON t.txid = o.txid "
            "WHERE o.is_mine = ?"
        )
        params = (int(mine),)
    rows = db.execute(query, params).fetchall()
    receives = []
    for txid, block_time, value_sat in rows:
        try:
            day = datetime.date.fromisoformat(str(block_time)[:10])
        except ValueError:
            continue
        receives.append((txid, day, int(value_sat)))
    return receives


def match_funding(
    withdrawals: list[Withdrawal],
    receives: list[tuple[str, datetime.date, int]],
    window_days: int = 3,
) -> FundingResult:
    """Match each exchange leg to chain outputs by exact sats + date window.

    Withdrawals (``out``) are receipt candidates; deposits (``in``) are
    external-send candidates — callers pass the matching receive pool.
    """
    matches: dict[str, str] = {}
    ambiguous: list[str] = []
    unmatched: list[str] = []
    for w in withdrawals:
        btc = Decimal(w.sats) / SAT_PER_BTC
        candidates = sorted(
            {
                txid
                for txid, day, sats in receives
                if sats == w.sats and abs((day - w.date).days) <= window_days
            }
        )
        kind = "withdrawal" if w.direction == "out" else "deposit"
        label = f"Kraken {kind} {w.refid} ({w.date.isoformat()}, {btc:f} BTC)"
        if len(candidates) == 1:
            matches.setdefault(candidates[0], label)
        elif candidates:
            ambiguous.append(f"{w.refid}: {len(candidates)} candidates ({btc:f} BTC)")
        else:
            unmatched.append(f"{w.refid} ({btc:f} BTC on {w.date.isoformat()})")
    return FundingResult(matches=matches, ambiguous=ambiguous, unmatched=unmatched)


def funding_from_ledgers(
    ledgers_path: str | Path | None,
    db: sqlite3.Connection,
    window_days: int = 3,
    trades_path: str | Path | None = None,
) -> FundingResult:
    """Convenience: parse ledgers + match against ``db`` (empty result when no path).

    Withdrawals match ours-only receipts, deposits match external-only sends.
    Raises the ledger parse error (e.g. margin leg without price and without
    ``trades_path``) instead of guessing — callers that treat funding as
    annotation-only catch it and continue unlabeled.
    """
    if not ledgers_path:
        return FundingResult(matches={})
    legs = kraken_btc_withdrawals(ledgers_path, trades_path)
    out = match_funding(
        [w for w in legs if w.direction == "out"], chain_receives(db, mine=True), window_days
    )
    inn = match_funding(
        [w for w in legs if w.direction == "in"], chain_receives(db, mine=False), window_days
    )
    return FundingResult(
        matches={**out.matches, **inn.matches},
        ambiguous=out.ambiguous + inn.ambiguous,
        unmatched=out.unmatched + inn.unmatched,
    )
