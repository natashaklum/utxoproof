"""Cross-domain UTXO saga: persisted links, confidence tiers, and coverage.

The on-chain DAG lives in :mod:`utxoproof.provenance`; this module joins it to
exchange ledgers and bank statements. Every edge points in the funding
direction::

    bank debit -> exchange EUR deposit -> trade -> BTC withdrawal -> chain UTXO

Derived edges are rebuilt idempotently. ``origin='user'`` rows always win, so a
manual confirmation or rejection survives the next rebuild. Confidence tiers
are ``exact`` / ``approximate`` / ``allocated`` / ``candidate`` (plus
``rejected`` for user rows); candidates are shown but never count as coverage.
"""

from __future__ import annotations

import datetime
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from utxoproof.banks import BankRow, parse_bank_csv
from utxoproof.exchange import ExchangeTx, FiatFlow
from utxoproof.kraken_csv import parse_kraken_fiat_flows, parse_kraken_ledgers
from utxoproof.matching import match_fiat_legs
from utxoproof.provenance import SagaNode, build_saga

SAT_PER_BTC = 100_000_000
LAYERS = ("bank", "exchange", "chain")
CONFIDENCE_TIERS = ("exact", "approximate", "allocated", "candidate", "rejected")
EVIDENCE_TIERS = frozenset({"exact", "approximate", "allocated"})
_RANK = {"exact": 0, "approximate": 1, "allocated": 2, "candidate": 3, "rejected": 4}
_LAYER_ORDER = {layer: i for i, layer in enumerate(LAYERS)}


@dataclass
class ProvenanceLink:
    """One persisted edge in a UTXO saga."""

    utxo_txid: str
    utxo_vout: int
    layer: str
    from_ref: str
    to_ref: str
    confidence: str
    evidence_ref: str
    amount_eur: Decimal | None = None
    amount_sat: int | None = None
    origin: str = "derived"
    note: str = ""
    created_at: str = ""
    id: int | None = None

    def __post_init__(self) -> None:
        if self.layer not in LAYERS:
            raise ValueError(f"unknown provenance layer {self.layer!r}")
        if self.confidence not in CONFIDENCE_TIERS:
            raise ValueError(f"unknown confidence tier {self.confidence!r}")
        if self.origin not in ("derived", "user"):
            raise ValueError(f"unknown provenance origin {self.origin!r}")


@dataclass
class SourceCoverage:
    """Per-input-less-ancestor coverage, for the provenance page."""

    txid: str
    vout: int
    share: Decimal
    tier: str  # exact | approximate | allocated | "" (no bank path)


@dataclass
class SagaDocument:
    """Everything the page needs from one all-parent, cross-domain walk."""

    txid: str
    vout: int
    nodes: list[SagaNode]
    truncated_share: Decimal
    links: list[ProvenanceLink] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    coverage: Decimal = Decimal(0)
    weakest_tier: str = ""
    source_coverage: list[SourceCoverage] = field(default_factory=list)

    @property
    def evidence_links(self) -> list[ProvenanceLink]:
        return [link for link in self.links if link.confidence in EVIDENCE_TIERS]

    @property
    def candidates(self) -> list[ProvenanceLink]:
        return [link for link in self.links if link.confidence == "candidate"]


@dataclass
class SagaSources:
    """Parsed ledgers and bank rows supplied to a saga rebuild."""

    transactions: list[ExchangeTx] = field(default_factory=list)
    fiat_flows: list[FiatFlow] = field(default_factory=list)
    bank_rows: list[BankRow] = field(default_factory=list)
    ledgers_file: str = ""
    bank_files: tuple[str, ...] = ()


def _now() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")


def _row_to_link(row: tuple[Any, ...]) -> ProvenanceLink:
    return ProvenanceLink(
        id=int(row[0]),
        utxo_txid=str(row[1]),
        utxo_vout=int(row[2]),
        layer=str(row[3]),
        from_ref=str(row[4]),
        to_ref=str(row[5]),
        confidence=str(row[6]),
        evidence_ref=str(row[7]),
        amount_eur=Decimal(row[8]) if row[8] is not None else None,
        amount_sat=int(row[9]) if row[9] is not None else None,
        origin=str(row[10]),
        note=str(row[11]),
        created_at=str(row[12]),
    )


def upsert_provenance_link(db: sqlite3.Connection, link: ProvenanceLink) -> None:
    """Insert/update one edge. Derived rows never overwrite ``origin='user'``."""
    db.execute(
        "INSERT INTO provenance_links "
        "(utxo_txid, utxo_vout, layer, from_ref, to_ref, confidence, evidence_ref, "
        "amount_eur, amount_sat, origin, note, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT (utxo_txid, utxo_vout, layer, from_ref, to_ref) DO UPDATE SET "
        "confidence=excluded.confidence, evidence_ref=excluded.evidence_ref, "
        "amount_eur=excluded.amount_eur, amount_sat=excluded.amount_sat, "
        "origin=excluded.origin, note=excluded.note, created_at=excluded.created_at "
        "WHERE provenance_links.origin <> 'user' OR excluded.origin = 'user'",
        (
            link.utxo_txid,
            link.utxo_vout,
            link.layer,
            link.from_ref,
            link.to_ref,
            link.confidence,
            link.evidence_ref,
            str(link.amount_eur) if link.amount_eur is not None else None,
            link.amount_sat,
            link.origin,
            link.note,
            link.created_at or _now(),
        ),
    )


def delete_derived_links(db: sqlite3.Connection, txid: str, vout: int) -> None:
    """Remove derived edges for one UTXO; user confirmations/rejections stay."""
    db.execute(
        "DELETE FROM provenance_links WHERE utxo_txid=? AND utxo_vout=? AND origin='derived'",
        (txid, vout),
    )


def list_provenance_links(db: sqlite3.Connection, txid: str, vout: int) -> list[ProvenanceLink]:
    rows = db.execute(
        "SELECT id, utxo_txid, utxo_vout, layer, from_ref, to_ref, confidence, "
        "evidence_ref, amount_eur, amount_sat, origin, note, created_at "
        "FROM provenance_links WHERE utxo_txid=? AND utxo_vout=? "
        "ORDER BY id",
        (txid, vout),
    ).fetchall()
    return [_row_to_link(row) for row in rows]


def set_user_link(
    db: sqlite3.Connection,
    link: ProvenanceLink,
    confidence: str | None = None,
    note: str | None = None,
) -> None:
    """Persist a user confirmation/rejection; it survives later rebuilds."""
    user_link = ProvenanceLink(
        utxo_txid=link.utxo_txid,
        utxo_vout=link.utxo_vout,
        layer=link.layer,
        from_ref=link.from_ref,
        to_ref=link.to_ref,
        confidence=confidence or link.confidence,
        evidence_ref=link.evidence_ref,
        amount_eur=link.amount_eur,
        amount_sat=link.amount_sat,
        origin="user",
        note=link.note if note is None else note,
        created_at=link.created_at,
        id=link.id,
    )
    upsert_provenance_link(db, user_link)


def _exchange_ref(kind: str, exchange: str, refid: str) -> str:
    return f"exchange:{(exchange or 'kraken').lower()}:{kind}:{refid}"


def _tx_ref(kind: str, tx: ExchangeTx) -> str:
    return _exchange_ref(kind, tx.exchange, tx.refid)


def _flow_ref(flow: FiatFlow) -> str:
    return _exchange_ref(flow.kind.lower(), flow.exchange, flow.refid)


def _chain_ref(node: SagaNode) -> str:
    return f"{node.txid}:{node.vout}"


def _bank_evidence(row: BankRow, index: int = 0) -> str:
    row_no = row.row_number or index
    return f"{Path(row.source_file).name}:{row_no}"


def bank_ref(row: BankRow, index: int = 0) -> str:
    """Stable bank-row identity; the CSV line survives re-sorting."""
    return f"bank:{_bank_evidence(row, index)}"


def _tx_evidence(tx: ExchangeTx) -> str:
    return tx.source_evidence or f"{(tx.exchange or 'kraken')}:refid:{tx.refid}"


def _flow_evidence(flow: FiatFlow) -> str:
    return flow.source_evidence or f"{flow.exchange}:refid:{flow.refid}"


def _evidence(*parts: str) -> str:
    return "; ".join(part for part in parts if part)


def _sats(tx: ExchangeTx) -> int:
    return int((tx.btc * SAT_PER_BTC).to_integral_value())


def _normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _reference_hit(refid: str, bank: BankRow) -> bool:
    needle = _normalize(refid)
    if not needle:
        return False
    return any(needle in _normalize(text) for text in (bank.reference or "", bank.description))


def _date_offset(bank: BankRow, flow: FiatFlow) -> int:
    return (bank.date - flow.date).days


def _fiat_direction_ok(bank: BankRow, flow: FiatFlow) -> bool:
    if flow.kind == "DEPOSIT":
        return bank.amount_eur < 0
    if flow.kind == "WITHDRAWAL":
        return bank.amount_eur > 0
    return False


def _tolerance(amount: Decimal) -> Decimal:
    return min(Decimal("1.00"), amount * Decimal("0.01"))


def _add_link(links: dict[tuple[str, str, str], ProvenanceLink], link: ProvenanceLink) -> None:
    key = (link.layer, link.from_ref, link.to_ref)
    existing = links.get(key)
    if existing is None or _RANK[link.confidence] < _RANK[existing.confidence]:
        links[key] = link


def _bank_link(
    txid: str,
    vout: int,
    bank: BankRow,
    bank_index: int,
    flow: FiatFlow,
    confidence: str,
    reasons: list[str],
) -> ProvenanceLink:
    return ProvenanceLink(
        utxo_txid=txid,
        utxo_vout=vout,
        layer="bank",
        from_ref=bank_ref(bank, bank_index),
        to_ref=_flow_ref(flow),
        confidence=confidence,
        evidence_ref=_evidence(_bank_evidence(bank, bank_index), _flow_evidence(flow)),
        amount_eur=flow.eur,
        note="; ".join(reasons),
    )


def match_bank_to_deposits(
    txid: str,
    vout: int,
    bank_rows: list[BankRow],
    flows: list[FiatFlow],
    window_days: int = 2,
) -> tuple[list[ProvenanceLink], list[str]]:
    """Bank statement rows -> exchange EUR deposits/withdrawals.

    Exact EUR equality wins, then a unique tolerance match of
    ``min(€1, 1%)``. A unique reference hit breaks ties inside a tier. Anything
    else stays unresolved; nothing is guessed.
    """
    links: list[ProvenanceLink] = []
    unresolved: list[str] = []
    used_bank: set[int] = set()
    blocked_bank: set[int] = set()
    used_flow: set[int] = set()
    flows = [flow for flow in flows if flow.eur > 0]

    def candidates(
        bank_index: int, bank: BankRow, approximate: bool
    ) -> list[tuple[FiatFlow, int, Decimal]]:
        out = []
        for flow in flows:
            if id(flow) in used_flow or not _fiat_direction_ok(bank, flow):
                continue
            offset = _date_offset(bank, flow)
            if abs(offset) > window_days:
                continue
            delta = abs(abs(bank.amount_eur) - flow.eur)
            if approximate:
                if delta == 0 or delta > _tolerance(flow.eur):
                    continue
            elif delta != 0:
                continue
            out.append((flow, offset, delta))
        return out

    for approximate in (False, True):
        for bank_index, bank in enumerate(bank_rows):
            if bank_index in used_bank or bank_index in blocked_bank or bank.amount_eur == 0:
                continue
            cands = candidates(bank_index, bank, approximate)
            if not cands:
                continue
            chosen: tuple[FiatFlow, int, Decimal] | None = None
            if len(cands) == 1:
                chosen = cands[0]
            else:
                ref_hits = [cand for cand in cands if _reference_hit(cand[0].refid, bank)]
                if len(ref_hits) == 1:
                    chosen = ref_hits[0]
            if chosen is None:
                unresolved.append(
                    f"{_bank_evidence(bank, bank_index)}: "
                    f"{len(cands)} ambiguous exchange deposit candidates"
                )
                blocked_bank.add(bank_index)
                continue
            flow, offset, delta = chosen
            confidence = "exact" if not approximate else "approximate"
            reasons = [
                f"date {offset:+d}d",
                (
                    f"amount EUR {flow.eur:,.2f} exact"
                    if not approximate
                    else f"amount EUR {flow.eur:,.2f} ~Δ€{delta:.2f}"
                ),
                "direction bank outflow<->deposit"
                if flow.kind == "DEPOSIT"
                else "direction bank inflow<->withdrawal",
            ]
            if _reference_hit(flow.refid, bank):
                reasons.append(f"reference {flow.refid}")
            links.append(_bank_link(txid, vout, bank, bank_index, flow, confidence, reasons))
            used_bank.add(bank_index)
            used_flow.add(id(flow))
    return links, unresolved


@dataclass
class _FiatLot:
    amount: Decimal
    source: FiatFlow | None


def allocate_deposits_to_trades(
    txid: str,
    vout: int,
    flows: list[FiatFlow],
    txs: list[ExchangeTx],
) -> tuple[list[ProvenanceLink], list[str], set[str]]:
    """Replay the EUR ledger balance and allocate deposit lots to buys.

    FIFO lots: a deposit enters on its date; a BUY consumes the oldest
    deposit (or sale-proceeds lot, which has no bank path); a SELL creates a
    proceeds lot. A buy funded by sale proceeds is therefore honestly not
    bank-covered. Returns links, unresolved notes, and trade refids that got
    at least one deposit lot.
    """
    links: list[ProvenanceLink] = []
    unresolved: list[str] = []
    allocated_trades: set[str] = set()
    deposits = [flow for flow in flows if flow.kind == "DEPOSIT" and flow.eur > 0]
    if not deposits:
        return links, unresolved, allocated_trades

    events: list[tuple[datetime.date, int, str, ExchangeTx | FiatFlow]] = []
    for flow in flows:
        order = 0 if flow.kind == "DEPOSIT" else 2
        events.append((flow.date, order, flow.refid, flow))
    for tx in txs:
        if tx.kind in ("BUY", "SELL") and tx.fiat_amount > 0:
            events.append((tx.date, 1, tx.refid, tx))
    events.sort(key=lambda event: (event[0], event[1], event[2]))

    lots: list[_FiatLot] = []
    for _, _, _, event in events:
        if isinstance(event, FiatFlow):
            if event.kind == "DEPOSIT":
                lots.append(_FiatLot(event.eur, event))
            elif event.kind == "WITHDRAWAL":
                _consume_fiat(lots, event.eur)
            continue
        if event.kind == "SELL":
            lots.append(_FiatLot(event.fiat_amount, None))
            continue
        remaining = event.fiat_amount
        while remaining > 0 and lots:
            lot = lots[0]
            take = min(remaining, lot.amount)
            lot.amount -= take
            remaining -= take
            if lot.source is not None:
                link = ProvenanceLink(
                    utxo_txid=txid,
                    utxo_vout=vout,
                    layer="exchange",
                    from_ref=_flow_ref(lot.source),
                    to_ref=_tx_ref("trade", event),
                    confidence="allocated",
                    evidence_ref=_evidence(_flow_evidence(lot.source), _tx_evidence(event)),
                    amount_eur=take,
                    note="FIFO EUR balance replay",
                )
                links.append(link)
                allocated_trades.add(event.refid)
            if lot.amount == 0:
                lots.pop(0)
        if remaining > 0:
            unresolved.append(
                f"trade {event.refid}: €{remaining:,.2f} unfunded by prior EUR deposits"
            )
    return links, unresolved, allocated_trades


def _consume_fiat(lots: list[_FiatLot], amount: Decimal) -> None:
    remaining = amount
    while remaining > 0 and lots:
        lot = lots[0]
        take = min(remaining, lot.amount)
        lot.amount -= take
        remaining -= take
        if lot.amount == 0:
            lots.pop(0)


@dataclass
class _BtcLot:
    amount_sat: int
    source_tx: ExchangeTx | None


def _btc_events(txs: list[ExchangeTx]) -> list[ExchangeTx]:
    return sorted(
        (tx for tx in txs if tx.btc > 0 or tx.kind in ("BUY", "DEPOSIT", "SELL", "WITHDRAWAL")),
        key=lambda tx: (tx.date, tx.refid),
    )


def _btc_balance_reliable(txs: list[ExchangeTx]) -> bool:
    available = 0
    for tx in _btc_events(txs):
        if tx.kind in ("BUY", "DEPOSIT"):
            available += _sats(tx)
        elif tx.kind in ("SELL", "WITHDRAWAL", "MARGIN", "ADJUSTMENT"):
            if _sats(tx) > available:
                return False
            available -= _sats(tx)
    return True


def _candidate_buys(
    withdrawal: ExchangeTx, buys: list[ExchangeTx], limit: int = 5
) -> list[ExchangeTx]:
    """Exact-sats buys before the withdrawal, nearest first (candidate tier)."""
    exact = [tx for tx in buys if _sats(tx) == _sats(withdrawal) and tx.date <= withdrawal.date]
    if exact:
        return sorted(exact, key=lambda tx: ((withdrawal.date - tx.date).days, tx.refid))[:limit]
    if not buys:
        return []
    before = [tx for tx in buys if tx.date <= withdrawal.date]
    before.sort(
        key=lambda tx: (
            abs(_sats(tx) - _sats(withdrawal)),
            (withdrawal.date - tx.date).days,
            tx.refid,
        )
    )
    return before[:limit]


def allocate_trades_to_withdrawals(
    txid: str,
    vout: int,
    txs: list[ExchangeTx],
) -> tuple[list[ProvenanceLink], list[str]]:
    """Replay the BTC ledger balance and allocate buy lots to withdrawals.

    With a usable balance, FIFO allocation produces ``allocated`` edges. If the
    ledger starts mid-history (any running balance would go negative), the
    withdrawal gets ``candidate`` edges to nearby prior buys instead; candidates
    never raise coverage.
    """
    links: list[ProvenanceLink] = []
    unresolved: list[str] = []
    events = _btc_events(txs)
    withdrawals = [tx for tx in events if tx.kind == "WITHDRAWAL"]
    buys = [tx for tx in events if tx.kind == "BUY"]
    if not withdrawals:
        return links, unresolved

    if not _btc_balance_reliable(events):
        for withdrawal in withdrawals:
            candidates = _candidate_buys(withdrawal, buys)
            if not candidates:
                unresolved.append(
                    f"withdrawal {withdrawal.refid}: ledger balance unusable and no prior buy"
                )
                continue
            unresolved.append(
                f"withdrawal {withdrawal.refid}: ledger balance unusable; "
                f"{len(candidates)} candidate buy(s), not asserted"
            )
            for buy in candidates:
                links.append(
                    ProvenanceLink(
                        utxo_txid=txid,
                        utxo_vout=vout,
                        layer="exchange",
                        from_ref=_tx_ref("trade", buy),
                        to_ref=_tx_ref("withdrawal", withdrawal),
                        confidence="candidate",
                        evidence_ref=_evidence(_tx_evidence(buy), _tx_evidence(withdrawal)),
                        amount_sat=_sats(buy),
                        note="BTC balance replay unusable; nearest prior exact-amount buy",
                    )
                )
        return links, unresolved

    lots: list[_BtcLot] = []
    for tx in events:
        if tx.kind in ("BUY", "DEPOSIT"):
            lots.append(_BtcLot(_sats(tx), tx if tx.kind == "BUY" else None))
            continue
        if tx.kind in ("SELL", "MARGIN", "ADJUSTMENT"):
            _consume_btc(lots, _sats(tx))
            continue
        if tx.kind != "WITHDRAWAL":
            continue
        remaining = _sats(tx)
        while remaining > 0 and lots:
            lot = lots[0]
            take = min(remaining, lot.amount_sat)
            lot.amount_sat -= take
            remaining -= take
            if lot.source_tx is not None:
                links.append(
                    ProvenanceLink(
                        utxo_txid=txid,
                        utxo_vout=vout,
                        layer="exchange",
                        from_ref=_tx_ref("trade", lot.source_tx),
                        to_ref=_tx_ref("withdrawal", tx),
                        confidence="allocated",
                        evidence_ref=_evidence(_tx_evidence(lot.source_tx), _tx_evidence(tx)),
                        amount_sat=take,
                        note="FIFO BTC balance replay",
                    )
                )
            if lot.amount_sat == 0:
                lots.pop(0)
        if remaining > 0:
            unresolved.append(
                f"withdrawal {tx.refid}: {remaining} sats from non-trade BTC "
                "(deposit/earn), no bank path"
            )
    return links, unresolved


def _consume_btc(lots: list[_BtcLot], amount_sat: int) -> None:
    remaining = amount_sat
    while remaining > 0 and lots:
        lot = lots[0]
        take = min(remaining, lot.amount_sat)
        lot.amount_sat -= take
        remaining -= take
        if lot.amount_sat == 0:
            lots.pop(0)


def match_withdrawals_to_chain(
    txid: str,
    vout: int,
    nodes: list[SagaNode],
    txs: list[ExchangeTx],
    window_days: int = 3,
) -> tuple[list[ProvenanceLink], list[str]]:
    """Exchange BTC withdrawals -> input-less chain receipts (exact then ~Δ)."""
    links: list[ProvenanceLink] = []
    unresolved: list[str] = []
    sources = [
        node
        for node in nodes
        if node.is_source
        and node.is_mine
        and not node.is_coinbase
        and node.source_type != "mining"
        and not node.event.startswith("Coinbase")
    ]
    withdrawals = [tx for tx in txs if tx.kind == "WITHDRAWAL" and tx.btc > 0]
    used_sources: set[tuple[str, int]] = set()
    used_withdrawals: set[str] = set()

    for approximate in (False, True):
        for withdrawal in withdrawals:
            if withdrawal.refid in used_withdrawals:
                continue
            w_sats = _sats(withdrawal)
            cands: list[tuple[SagaNode, int, int]] = []
            for node in sources:
                key = (node.txid, node.vout)
                if key in used_sources:
                    continue
                delta = abs(node.amount_sat - w_sats)
                if not approximate and delta != 0:
                    continue
                if approximate and (delta == 0 or delta > max(1000, w_sats // 1000)):
                    continue
                offset = (node.block_time.date() - withdrawal.date).days
                if abs(offset) > window_days:
                    continue
                cands.append((node, delta, offset))
            if not cands:
                continue
            if len(cands) != 1:
                unresolved.append(
                    f"withdrawal {withdrawal.refid}: "
                    f"{len(cands)} {'approximate ' if approximate else ''}chain candidates"
                )
                used_withdrawals.add(withdrawal.refid)
                continue
            node, delta, offset = cands[0]
            confidence = "exact" if not approximate else "approximate"
            note = (
                f"date {offset:+d}d; amount {w_sats} sats exact"
                if not approximate
                else f"date {offset:+d}d; amount ~Δ{delta} sats"
            )
            links.append(
                ProvenanceLink(
                    utxo_txid=txid,
                    utxo_vout=vout,
                    layer="chain",
                    from_ref=_tx_ref("withdrawal", withdrawal),
                    to_ref=_chain_ref(node),
                    confidence=confidence,
                    evidence_ref=_evidence(_tx_evidence(withdrawal), f"{node.txid}:{node.vout}"),
                    amount_sat=node.amount_sat,
                    note=note,
                )
            )
            used_sources.add((node.txid, node.vout))
            used_withdrawals.add(withdrawal.refid)

    for node in sources:
        if (node.txid, node.vout) in used_sources:
            continue
        if node.source_type in ("exchange_purchase", "p2p_purchase"):
            unresolved.append(
                f"source {node.txid}:{node.vout}: no exchange withdrawal within "
                f"±{window_days}d at its sats"
            )
    return links, unresolved


def _direct_bank_to_trade_links(
    txid: str,
    vout: int,
    bank_rows: list[BankRow],
    txs: list[ExchangeTx],
    allocated_trade_refs: set[str],
) -> tuple[list[ProvenanceLink], list[str]]:
    """Fallback for ledgers without EUR deposit rows: bank debit -> trade.

    This keeps the v6 direct matcher useful while the deposit layer is absent
    (or when the ledger export starts after the funding deposit). ``txs`` is
    already scoped to trades on the target path.
    """
    trades = [
        tx
        for tx in txs
        if tx.kind in ("BUY", "SELL") and _tx_ref("trade", tx) not in allocated_trade_refs
    ]
    if not trades or not bank_rows:
        return [], []
    matches, _ = match_fiat_legs(trades, bank_rows)
    index_by_id = {id(row): i for i, row in enumerate(bank_rows)}
    links = []
    for match in matches:
        bank_index = index_by_id[id(match.bank)]
        links.append(
            ProvenanceLink(
                utxo_txid=txid,
                utxo_vout=vout,
                layer="bank",
                from_ref=bank_ref(match.bank, bank_index),
                to_ref=_tx_ref("trade", match.tx),
                confidence="exact" if match.tier != "approximate" else "approximate",
                evidence_ref=_evidence(
                    _bank_evidence(match.bank, bank_index),
                    _tx_evidence(match.tx),
                ),
                amount_eur=match.tx.fiat_amount,
                note="; ".join(match.reasons),
            )
        )
    return links, []


def build_saga_links(
    txid: str,
    vout: int,
    nodes: list[SagaNode],
    sources: SagaSources,
) -> tuple[list[ProvenanceLink], list[str]]:
    """Build only the cross-domain edges on this target's funding path."""
    links: list[ProvenanceLink] = []
    unresolved: list[str] = []

    # Match the chain receipt first; everything below is scoped to the
    # withdrawal(s) that actually fund this target. A busy ledger therefore
    # never spills unrelated trades or bank rows onto every UTXO page.
    chain_links, chain_notes = match_withdrawals_to_chain(txid, vout, nodes, sources.transactions)
    links.extend(chain_links)
    unresolved.extend(chain_notes)
    matched_withdrawals = {link.from_ref for link in chain_links}
    if not matched_withdrawals:
        return links, unresolved
    matched_withdrawal_ids = {ref.rsplit(":", 1)[-1] for ref in matched_withdrawals}

    withdrawal_links, withdrawal_notes = allocate_trades_to_withdrawals(
        txid, vout, sources.transactions
    )
    withdrawal_links = [link for link in withdrawal_links if link.to_ref in matched_withdrawals]
    links.extend(withdrawal_links)
    unresolved.extend(
        note
        for note in withdrawal_notes
        if any(f"withdrawal {refid}" in note for refid in matched_withdrawal_ids)
    )
    trade_refs = {link.from_ref for link in withdrawal_links if link.confidence in EVIDENCE_TIERS}

    deposit_links, deposit_notes, _ = allocate_deposits_to_trades(
        txid, vout, sources.fiat_flows, sources.transactions
    )
    deposit_links = [link for link in deposit_links if link.to_ref in trade_refs]
    links.extend(deposit_links)
    trade_ids = {ref.rsplit(":", 1)[-1] for ref in trade_refs}
    unresolved.extend(
        note for note in deposit_notes if any(f"trade {refid}" in note for refid in trade_ids)
    )
    deposit_refs = {link.from_ref for link in deposit_links}

    bank_links, bank_notes = match_bank_to_deposits(
        txid, vout, sources.bank_rows, sources.fiat_flows
    )
    bank_links = [link for link in bank_links if link.to_ref in deposit_refs]
    links.extend(bank_links)
    if not deposit_refs:
        # No deposit path, so bank-row ambiguity cannot explain this target.
        bank_notes = []
    unresolved.extend(bank_notes)

    used_bank_refs = {link.from_ref for link in bank_links}
    unused_bank_rows = [
        row
        for index, row in enumerate(sources.bank_rows)
        if bank_ref(row, index) not in used_bank_refs
    ]
    allocated_trade_refs = {link.to_ref for link in deposit_links}
    scoped_txs = [tx for tx in sources.transactions if _tx_ref("trade", tx) in trade_refs]
    direct_links, direct_notes = _direct_bank_to_trade_links(
        txid, vout, unused_bank_rows, scoped_txs, allocated_trade_refs
    )
    links.extend(direct_links)
    unresolved.extend(direct_notes)

    # Deduplicate edges while keeping the strongest confidence.
    unique: dict[tuple[str, str, str], ProvenanceLink] = {}
    for link in links:
        _add_link(unique, link)
    ordered = sorted(
        unique.values(),
        key=lambda link: (
            _LAYER_ORDER.get(link.layer, 99),
            _RANK.get(link.confidence, 99),
            link.from_ref,
            link.to_ref,
        ),
    )
    return ordered, list(dict.fromkeys(unresolved))


def _worse(a: str, b: str) -> str:
    if not a:
        return b
    if not b:
        return a
    return a if _RANK[a] >= _RANK[b] else b


def _best_bank_tier(
    start: str,
    reverse: dict[str, list[tuple[str, str]]],
    bank_refs: set[str],
) -> str:
    """Weakest tier on the best path from a chain node back to a bank row."""
    best_here: dict[str, str] = {}
    best: str = ""
    stack: list[tuple[str, str]] = [(start, "")]
    while stack:
        current, weak = stack.pop()
        if current in bank_refs:
            best = _worse(best, weak)
            continue
        previous = best_here.get(current)
        if previous is not None and _RANK[previous] <= _RANK[weak]:
            continue
        best_here[current] = weak
        for before, confidence in reverse.get(current, []):
            if confidence in EVIDENCE_TIERS:
                stack.append((before, _worse(weak, confidence)))
    return best


def saga_coverage(
    nodes: list[SagaNode], links: list[ProvenanceLink]
) -> tuple[Decimal, str, list[SourceCoverage]]:
    """Value-weighted coverage: shares whose path reaches a bank row.

    Only exact/approximate/allocated edges count. A source with no path is
    listed with an empty tier so the page can show the missing value.
    """
    reverse: dict[str, list[tuple[str, str]]] = {}
    bank_refs: set[str] = set()
    for link in links:
        if link.confidence not in EVIDENCE_TIERS:
            continue
        reverse.setdefault(link.to_ref, []).append((link.from_ref, link.confidence))
        if link.layer == "bank":
            bank_refs.add(link.from_ref)
    coverage = Decimal(0)
    weakest = ""
    rows: list[SourceCoverage] = []
    for node in nodes:
        if not node.is_source or node.share <= 0:
            continue
        tier = _best_bank_tier(_chain_ref(node), reverse, bank_refs)
        rows.append(SourceCoverage(node.txid, node.vout, node.share, tier))
        if tier:
            coverage += node.share
            weakest = _worse(weakest, tier)
    return coverage, weakest, rows


def load_saga_sources(
    ledgers_path: str | Path | None,
    trades_path: str | Path | None = None,
    bank_specs: list[tuple[str | Path, str]] | None = None,
) -> SagaSources | None:
    """Parse the files needed for a rebuild; ``None`` when nothing was supplied."""
    if not ledgers_path and not bank_specs:
        return None
    transactions: list[ExchangeTx] = []
    fiat_flows: list[FiatFlow] = []
    ledgers_file = ""
    if ledgers_path:
        path = Path(ledgers_path)
        transactions = parse_kraken_ledgers(path, trades_path)
        fiat_flows = parse_kraken_fiat_flows(path)
        ledgers_file = path.name
    bank_rows: list[BankRow] = []
    bank_files: set[str] = set()
    for bank_path, bank_type in bank_specs or []:
        bank_rows.extend(parse_bank_csv(bank_path, bank_type))
        bank_files.add(Path(bank_path).name)
    bank_rows.sort(key=lambda row: (row.date.isoformat(), row.source_file, row.row_number))
    return SagaSources(
        transactions=transactions,
        fiat_flows=fiat_flows,
        bank_rows=bank_rows,
        ledgers_file=ledgers_file,
        bank_files=tuple(sorted(bank_files)),
    )


def build_saga_document(
    db: sqlite3.Connection,
    txid: str,
    vout: int,
    price_at: Callable[[datetime.date], Decimal],
    sources: SagaSources | None = None,
    max_depth: int = 100,
    max_nodes: int = 500,
) -> SagaDocument:
    """Walk the DAG, optionally rebuild links, and compute coverage."""
    nodes, truncated = build_saga(txid, vout, db, price_at, max_depth, max_nodes)
    unresolved: list[str] = []
    if sources is not None:
        delete_derived_links(db, txid, vout)
        built, unresolved = build_saga_links(txid, vout, nodes, sources)
        for link in built:
            upsert_provenance_link(db, link)
        db.commit()
    # Read back so user-confirmed/rejected rows are included in coverage.
    links = list_provenance_links(db, txid, vout)
    coverage, weakest, source_coverage = saga_coverage(nodes, links)
    return SagaDocument(
        txid=txid,
        vout=vout,
        nodes=nodes,
        truncated_share=truncated,
        links=links,
        unresolved=unresolved,
        coverage=coverage,
        weakest_tier=weakest,
        source_coverage=source_coverage,
    )


__all__ = [
    "CONFIDENCE_TIERS",
    "EVIDENCE_TIERS",
    "LAYERS",
    "ProvenanceLink",
    "SagaDocument",
    "SagaSources",
    "SourceCoverage",
    "allocate_deposits_to_trades",
    "allocate_trades_to_withdrawals",
    "bank_ref",
    "build_saga_document",
    "build_saga_links",
    "delete_derived_links",
    "list_provenance_links",
    "load_saga_sources",
    "match_bank_to_deposits",
    "match_withdrawals_to_chain",
    "saga_coverage",
    "set_user_link",
    "upsert_provenance_link",
]
