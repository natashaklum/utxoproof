"""Exchange-withdrawal to on-chain funding matcher (plan v5 Sec. 10).

Kraken ledgers carry no on-chain txid, so the Kraken<->chain join is amount +
date: a withdrawal of S BTC on day D funded the on-chain transaction that
received exactly S into the wallet within +/- ``window_days``. Exact
single-candidate matches annotate provenance steps; ambiguous or unmatched
withdrawals are reported, never guessed.
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


@dataclass
class FundingResult:
    """Outcome of matching Kraken withdrawals to chain receives."""

    matches: dict[str, str]  # chain txid -> funding label for provenance steps
    ambiguous: list[str] = field(default_factory=list)  # refids, human-readable
    unmatched: list[str] = field(default_factory=list)  # refids, human-readable


def kraken_btc_withdrawals(
    ledgers_path: str | Path, trades_path: str | Path | None = None
) -> list[Withdrawal]:
    """BTC withdrawal legs from a Kraken ``ledgers.csv`` export.

    ``trades_path`` is the same join ``import --trades`` takes: without it,
    margin-disposal legs that need execution prices raise instead of parsing.
    """
    from utxoproof.kraken_csv import parse_kraken_ledgers

    out = []
    for tx in parse_kraken_ledgers(Path(ledgers_path), trades_path):
        if tx.kind == "WITHDRAWAL" and tx.btc > 0:
            out.append(
                Withdrawal(
                    refid=tx.refid,
                    date=tx.date,
                    sats=int((tx.btc * SAT_PER_BTC).to_integral_value()),
                )
            )
    return out


def chain_receives(db: sqlite3.Connection) -> list[tuple[str, datetime.date, int]]:
    """Every on-chain output as ``(creating txid, tx date, value sats)``."""
    rows = db.execute(
        "SELECT o.txid, t.block_time, o.value_sat "
        "FROM tx_outputs o JOIN transactions t ON t.txid = o.txid"
    ).fetchall()
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
    """Match each withdrawal to chain receives by exact sats + date window."""
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
        label = f"Kraken withdrawal {w.refid} ({w.date.isoformat()}, {btc:f} BTC)"
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

    Raises the ledger parse error (e.g. margin leg without price and without
    ``trades_path``) instead of guessing — callers that treat funding as
    annotation-only catch it and continue unlabeled.
    """
    if not ledgers_path:
        return FundingResult(matches={})
    return match_funding(
        kraken_btc_withdrawals(ledgers_path, trades_path), chain_receives(db), window_days
    )
