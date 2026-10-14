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

from utxoproof.exchange import ExchangeTx
from utxoproof.matching import FiatMatch

SAT_PER_BTC = 100_000_000


@dataclass
class Withdrawal:
    refid: str
    date: datetime.date
    sats: int
    direction: str = "out"  # out = exchange->wallet (withdrawal); in = wallet->exchange (deposit)


@dataclass
class TrailCandidate:
    """One candidate answer to 'which buy funded this withdrawal'.

    Never asserted as fact: exact-BTC-amount buys predating the withdrawal,
    nearest first. ``bank`` is the fiat match for the buy (with reasons) or
    ``None`` when no bank row matched it.
    """

    chain_txid: str
    withdrawal_refid: str
    withdrawal_date: str
    buy_refid: str
    buy_date: str
    buy_btc: str
    buy_fiat_eur: str
    bank: FiatMatch | None = None
    hidden_count: int = 0
    ledgers_file: str = ""
    bank_file: str = ""


@dataclass
class FundingResult:
    """Outcome of matching Kraken legs to chain receives/spends."""

    matches: dict[str, str]  # chain txid -> funding label for provenance steps
    ambiguous: list[str] = field(default_factory=list)  # refids, human-readable
    unmatched: list[str] = field(default_factory=list)  # refids, human-readable


def legs_from_txs(txs: list[ExchangeTx]) -> list[Withdrawal]:
    """BTC exchange<->wallet legs from parsed ledger transactions."""
    out = []
    for tx in txs:
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


def kraken_btc_withdrawals(
    ledgers_path: str | Path, trades_path: str | Path | None = None
) -> list[Withdrawal]:
    """BTC exchange<->wallet legs from a Kraken ``ledgers.csv`` export.

    ``trades_path`` is the same join ``import --trades`` takes: without it,
    margin-disposal legs that need execution prices raise instead of parsing.
    """
    from utxoproof.kraken_csv import parse_kraken_ledgers

    return legs_from_txs(parse_kraken_ledgers(Path(ledgers_path), trades_path))


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


def funding_trail(
    ledgers_path: str | Path | None,
    db: sqlite3.Connection,
    bank_csv: str | Path | None,
    bank_type: str = "kbc",
    trades_path: str | Path | None = None,
    window_days: int = 3,
    max_candidates: int = 5,
) -> list[TrailCandidate]:
    """Candidate answers to 'which buy funded this withdrawal' (never asserted).

    For every chain tx funding-matched to a Kraken withdrawal, list exact-BTC
    buys predating it, nearest first, each with its bank fiat match (or none).
    Needs ledgers + bank CSV; either missing yields no candidates (empty list).
    Raises ledger/bank parse errors instead of guessing — callers catch them.
    """
    from utxoproof.banks import parse_bank_csv
    from utxoproof.kraken_csv import parse_kraken_ledgers
    from utxoproof.matching import match_fiat_legs

    if not ledgers_path or not bank_csv:
        return []
    txs = parse_kraken_ledgers(Path(ledgers_path), trades_path)
    ledgers_name = Path(ledgers_path).name
    bank_name = Path(bank_csv).name
    funding = funding_from_ledgers(ledgers_path, db, window_days, trades_path)
    funded = {
        txid for txid, label in funding.matches.items() if label.startswith("Kraken withdrawal ")
    }
    if not funded:
        return []
    buys = [tx for tx in txs if tx.kind == "BUY" and tx.btc > 0]
    buy_sats = {id(tx): int((tx.btc * SAT_PER_BTC).to_integral_value()) for tx in buys}
    matches, _ = match_fiat_legs(txs, parse_bank_csv(bank_csv, bank_type))
    match_by_tx = {id(m.tx): m for m in matches}
    withdrawals = [w for w in legs_from_txs(txs) if w.direction == "out"]
    by_refid = {w.refid: w for w in withdrawals}
    trail: list[TrailCandidate] = []
    for chain_txid in sorted(funded):
        label = funding.matches[chain_txid]
        refid = label.removeprefix("Kraken withdrawal ").split(" (", 1)[0]
        w = by_refid.get(refid)
        if w is None:
            continue
        cands = sorted(
            (tx for tx in buys if buy_sats[id(tx)] == w.sats and tx.date <= w.date),
            key=lambda tx: ((w.date - tx.date).days, tx.refid),
        )
        hidden = max(0, len(cands) - max_candidates)
        for tx in cands[:max_candidates]:
            match = match_by_tx.get(id(tx))
            fiat = f"{tx.fiat_amount:,.2f}" if tx.eur_per_btc > 0 else "—"
            trail.append(
                TrailCandidate(
                    chain_txid=chain_txid,
                    withdrawal_refid=w.refid,
                    withdrawal_date=w.date.isoformat(),
                    buy_refid=tx.refid,
                    buy_date=tx.date.isoformat(),
                    buy_btc=f"{tx.btc:f}",
                    buy_fiat_eur=fiat,
                    bank=match,
                    hidden_count=hidden,
                    ledgers_file=ledgers_name,
                    bank_file=bank_name,
                )
            )
    return trail
