"""UTXO provenance: chain-of-custody graph traversal (plan Sec. 14).

``build_provenance_chain`` is the shipped v6 walk: it follows the largest
parent input at each step. ``build_saga`` is the v7 all-parent DAG walk: every
parent contributes a proportional share, and value that hits a cap or a
missing parent is returned as a truncated share instead of being hidden.
Callers supply ``price_at(date)`` so tests stay offline; the CLI wires the
oracle.
"""

from __future__ import annotations

import datetime
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

SAT_PER_BTC = 100_000_000


def _funding_evidence(existing: str, txid: str, funding: dict[str, str] | None) -> str:
    """Attach an exchange-funding label to a step's evidence (plan v5 Sec. 10)."""
    label = (funding or {}).get(txid, "")
    if not label:
        return existing
    return f"{existing}; {label}" if existing else label


@dataclass
class ProvenanceStep:
    step_number: int
    txid: str
    vout: int
    block_height: int | None
    block_time: datetime.datetime
    amount_sat: int
    amount_btc: Decimal
    eur_price: Decimal
    eur_value: Decimal
    kyc_status: str
    kyc_fraction: Decimal
    source_type: str
    source_label: str
    source_evidence: str
    event_description: str
    mixing_event: bool
    parent_utxos: list[tuple[str, int]] = field(default_factory=list)
    sibling_utxos: list[tuple[str, int]] = field(default_factory=list)


def db_get_output(db: sqlite3.Connection, txid: str, vout: int) -> dict[str, Any] | None:
    row = db.execute(
        "SELECT txid, vout, address, value_sat, kyc_status, kyc_fraction, "
        "source_type, source_label, source_evidence, block_height, block_time "
        "FROM tx_outputs JOIN transactions USING (txid) "
        "WHERE tx_outputs.txid=? AND vout=?",
        (txid, vout),
    ).fetchone()
    if row is None:
        return None
    keys = (
        "txid",
        "vout",
        "address",
        "value_sat",
        "kyc_status",
        "kyc_fraction",
        "source_type",
        "source_label",
        "source_evidence",
        "block_height",
        "block_time",
    )
    return dict(zip(keys, row, strict=True))


def db_get_inputs_for_tx(db: sqlite3.Connection, txid: str) -> list[dict[str, Any]]:
    rows = db.execute(
        "SELECT prev_txid, prev_vout FROM tx_inputs WHERE txid=? ORDER BY input_index",
        (txid,),
    ).fetchall()
    return [{"prev_txid": r[0], "prev_vout": r[1]} for r in rows]


def db_get_outputs_for_tx(db: sqlite3.Connection, txid: str) -> list[dict[str, Any]]:
    rows = db.execute(
        "SELECT txid, vout FROM tx_outputs WHERE txid=? ORDER BY vout", (txid,)
    ).fetchall()
    return [{"txid": r[0], "vout": r[1]} for r in rows]


def classify_event(
    source_type: str | None,
    source_label: str | None,
    n_inputs: int,
    n_outputs: int,
) -> str:
    """Human-readable on-chain event description (spec Sec. 14)."""
    if source_type == "exchange_purchase":
        return f"Exchange purchase ({source_label})" if source_label else "Exchange purchase"
    if source_type == "p2p_purchase":
        return f"P2P purchase ({source_label})" if source_label else "P2P purchase"
    if n_inputs == 0:
        return "Coinbase reward (mining)"
    if n_inputs == 1 and n_outputs == 1:
        return "Self-transfer (simple move between wallets)"
    if n_inputs > 1 and n_outputs <= 2:
        return f"Consolidation ({n_inputs} inputs merged)"
    if n_inputs == 1 and n_outputs > 2:
        return f"Fan-out ({n_outputs} outputs created)"
    if n_outputs == 2:
        return "Spend (payment + change)"
    return f"On-chain transaction ({n_inputs} inputs, {n_outputs} outputs)"


@dataclass
class SagaNode:
    """One output in the backward ancestry DAG of a target UTXO.

    ``share`` is the fraction of the **target's** value that flowed through
    this output. It is not a fraction of this output's own value. ``depth`` is
    0 for the target, 1 for its direct parents, and so on.
    """

    txid: str
    vout: int
    block_height: int | None
    block_time: datetime.datetime
    amount_sat: int
    share: Decimal
    depth: int
    kyc_status: str
    event: str
    evidence: str = ""
    # Facts carried for the future saga renderer, kept out of the v7 sketch's
    # minimal shape but already useful to tests and cross-domain linking.
    amount_btc: Decimal = Decimal(0)
    eur_price: Decimal = Decimal(0)
    eur_value: Decimal = Decimal(0)
    kyc_fraction: Decimal = Decimal("0")
    source_type: str = "unknown"
    source_label: str = ""
    mixing_event: bool = False
    is_source: bool = False
    parent_utxos: tuple[tuple[str, int], ...] = ()
    sibling_utxos: tuple[tuple[str, int], ...] = ()
    fee_sat: int = 0


def _parse_block_time(raw: str) -> datetime.datetime:
    parsed = datetime.datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.UTC)
    return parsed


def _tx_is_coinbase(db: sqlite3.Connection, txid: str) -> bool:
    row = db.execute("SELECT is_coinbase FROM transactions WHERE txid=?", (txid,)).fetchone()
    return bool(row[0]) if row is not None else False


def _tx_output_total_sat(db: sqlite3.Connection, txid: str) -> int:
    row = db.execute(
        "SELECT COALESCE(SUM(value_sat), 0) FROM tx_outputs WHERE txid=?", (txid,)
    ).fetchone()
    return int(row[0]) if row is not None else 0


def _saga_event(
    source_type: str | None,
    source_label: str | None,
    n_inputs: int,
    n_outputs: int,
    is_coinbase: bool,
) -> str:
    """Event label for a saga node, without calling every input-less receipt mining."""
    if n_inputs == 0:
        if source_type in {"exchange_purchase", "p2p_purchase"}:
            return classify_event(source_type, source_label, n_inputs, n_outputs)
        if is_coinbase or source_type == "mining":
            return "Coinbase reward (mining)"
        return "External receipt (no on-chain parent)"
    return classify_event(source_type, source_label, n_inputs, n_outputs)


def _append_evidence(existing: str, note: str) -> str:
    if not note:
        return existing
    return f"{existing}; {note}" if existing else note


def build_saga(
    txid: str,
    vout: int,
    db: sqlite3.Connection,
    price_at: Callable[[datetime.date], Decimal],
    max_depth: int = 100,
    max_nodes: int = 500,
) -> tuple[list[SagaNode], Decimal]:
    """Walk the full ancestor DAG of ``txid:vout`` with proportional shares.

    share(parent) = share(child) * value(parent) / sum(input values of tx)

    Nodes are keyed by ``(txid, vout)``; shares reaching the same node in one
    level are summed before it is expanded. The returned list is oldest-first
    (same-height ties by txid:vout) and includes the target. The second return
    value is the fraction of the target's value that hit ``max_depth``,
    ``max_nodes``, a missing parent, or a corrupt cycle. That value must be
    shown as missing, never as complete.

    ``max_depth`` counts nodes on a path: 1 returns only the target, 2 returns
    the target plus its direct parents. ``max_nodes`` is a hard cap; when a
    level is too large the largest shares are kept and the rest are truncated.
    """
    if max_depth < 1:
        raise ValueError("max_depth must be >= 1")
    if max_nodes < 1:
        raise ValueError("max_nodes must be >= 1")

    target = db_get_output(db, txid, vout)
    if target is None:
        raise ValueError(f"unknown UTXO {txid}:{vout}")

    nodes: dict[tuple[str, int], SagaNode] = {}
    fees: dict[str, int] = {}
    frontier: dict[tuple[str, int], Decimal] = {(txid, vout): Decimal(1)}
    truncated = Decimal(0)
    depth = 0

    while frontier:
        # A corrupt graph can reach an already-expanded node. Do not loop;
        # the duplicate share is unresolved value.
        clean_frontier: dict[tuple[str, int], Decimal] = {}
        for key, share in frontier.items():
            if key in nodes:
                truncated += share
            else:
                clean_frontier[key] = share
        frontier = clean_frontier
        if not frontier:
            break

        capacity = max_nodes - len(nodes)
        if capacity <= 0:
            truncated += sum(frontier.values(), Decimal(0))
            break
        if len(frontier) > capacity:
            ordered = sorted(
                frontier.items(),
                key=lambda item: (-item[1], item[0][0], item[0][1]),
            )
            kept = dict(ordered[:capacity])
            truncated += sum((share for _, share in ordered[capacity:]), Decimal(0))
            frontier = kept

        next_frontier: dict[tuple[str, int], Decimal] = {}
        for key, share in frontier.items():
            output = db_get_output(db, key[0], key[1])
            if output is None:
                truncated += share
                continue

            block_time = _parse_block_time(str(output["block_time"]))
            amount_sat = int(output["value_sat"])
            amount_btc = Decimal(amount_sat) / Decimal(SAT_PER_BTC)
            eur_price = price_at(block_time.date())

            inputs = db_get_inputs_for_tx(db, key[0])
            parent_utxos = tuple(
                (str(input_row["prev_txid"]), int(input_row["prev_vout"])) for input_row in inputs
            )
            parent_rows: list[tuple[dict[str, Any], dict[str, Any] | None]] = [
                (input_row, db_get_output(db, input_row["prev_txid"], input_row["prev_vout"]))
                for input_row in inputs
            ]
            all_parents_known = all(parent is not None for _, parent in parent_rows)
            parent_total = sum(
                int(parent["value_sat"]) for _, parent in parent_rows if parent is not None
            )
            missing_parents = [
                (str(input_row["prev_txid"]), int(input_row["prev_vout"]))
                for input_row, parent in parent_rows
                if parent is None
            ]

            all_outputs = db_get_outputs_for_tx(db, key[0])
            sibling_utxos = tuple(
                (str(output_row["txid"]), int(output_row["vout"]))
                for output_row in all_outputs
                if int(output_row["vout"]) != key[1]
            )
            parent_statuses = {
                str(parent["kyc_status"]) for _, parent in parent_rows if parent is not None
            }

            if key[0] not in fees:
                if inputs and all_parents_known:
                    fees[key[0]] = max(0, parent_total - _tx_output_total_sat(db, key[0]))
                else:
                    fees[key[0]] = 0

            evidence = str(output["source_evidence"] or "")
            if missing_parents:
                missing = ", ".join(
                    f"{parent_txid}:{parent_vout}" for parent_txid, parent_vout in missing_parents
                )
                evidence = _append_evidence(evidence, f"missing parent {missing}")

            is_source = not inputs
            nodes[key] = SagaNode(
                txid=str(output["txid"]),
                vout=int(output["vout"]),
                block_height=output["block_height"],
                block_time=block_time,
                amount_sat=amount_sat,
                share=share,
                depth=depth,
                kyc_status=str(output["kyc_status"]),
                event=_saga_event(
                    output["source_type"],
                    output["source_label"],
                    len(inputs),
                    len(all_outputs),
                    _tx_is_coinbase(db, key[0]) if is_source else False,
                ),
                evidence=evidence,
                amount_btc=amount_btc,
                eur_price=eur_price,
                eur_value=amount_btc * eur_price,
                kyc_fraction=Decimal(output["kyc_fraction"]),
                source_type=str(output["source_type"] or "unknown"),
                source_label=str(output["source_label"] or ""),
                mixing_event=len(parent_statuses) > 1,
                is_source=is_source,
                parent_utxos=parent_utxos,
                sibling_utxos=sibling_utxos,
                fee_sat=fees[key[0]],
            )

            if is_source:
                continue
            if not all_parents_known:
                # We cannot split the child's value honestly without every
                # parent amount, so the whole node is unresolved.
                truncated += share
                continue
            if parent_total <= 0:
                truncated += share
                continue

            for input_row, parent in parent_rows:
                if parent is None:  # already handled by all_parents_known
                    continue
                parent_share = share * Decimal(int(parent["value_sat"])) / Decimal(parent_total)
                parent_key = (str(input_row["prev_txid"]), int(input_row["prev_vout"]))
                next_frontier[parent_key] = next_frontier.get(parent_key, Decimal(0)) + parent_share

        depth += 1
        if depth >= max_depth and next_frontier:
            truncated += sum(next_frontier.values(), Decimal(0))
            next_frontier.clear()
        frontier = next_frontier

    ordered_nodes = sorted(
        nodes.values(),
        key=lambda node: (
            node.block_time,
            -1 if node.block_height is None else node.block_height,
            node.txid,
            node.vout,
        ),
    )
    return ordered_nodes, truncated


def saga_source_share(nodes: list[SagaNode]) -> Decimal:
    """On-chain source share: value whose ancestry reaches an input-less output.

    This is only the chain portion of the v7 coverage metric; cross-domain
    coverage additionally requires a bank path through exact/approximate/
    allocated links.
    """
    return sum((node.share for node in nodes if node.is_source), Decimal(0))


def build_provenance_chain(
    txid: str,
    vout: int,
    db: sqlite3.Connection,
    price_at: Callable[[datetime.date], Decimal],
    max_depth: int = 100,
    funding: dict[str, str] | None = None,
) -> list[ProvenanceStep]:
    """Walk back from ``txid:vout`` to origin. Returns steps oldest-first.

    ``funding`` maps a chain txid to an exchange-funding label (see
    ``utxoproof.linking``); matching steps carry it as source evidence so the
    provenance view shows which withdrawal funded the receipt.
    """
    steps: list[ProvenanceStep] = []
    visited: set[tuple[str, int]] = set()
    current: tuple[str, int] | None = (txid, vout)

    def _value_sat(ref: tuple[str, int]) -> int:
        row = db_get_output(db, ref[0], ref[1])
        return row["value_sat"] if row else 0

    while current and len(steps) < max_depth:
        if current in visited:
            break
        visited.add(current)
        output = db_get_output(db, current[0], current[1])
        if output is None:
            break

        block_time = _parse_block_time(output["block_time"])
        eur_price = price_at(block_time.date())
        amount_btc = Decimal(output["value_sat"]) / Decimal(SAT_PER_BTC)

        inputs = db_get_inputs_for_tx(db, output["txid"])
        parent_utxos = [(i["prev_txid"], i["prev_vout"]) for i in inputs]
        all_outputs = db_get_outputs_for_tx(db, output["txid"])
        sibling_utxos = [(o["txid"], o["vout"]) for o in all_outputs if o["vout"] != output["vout"]]
        parent_statuses = {
            (db_get_output(db, p[0], p[1]) or {}).get("kyc_status") for p in parent_utxos
        } - {None}

        steps.append(
            ProvenanceStep(
                step_number=0,  # renumbered after reversal
                txid=output["txid"],
                vout=output["vout"],
                block_height=output["block_height"],
                block_time=block_time,
                amount_sat=output["value_sat"],
                amount_btc=amount_btc,
                eur_price=eur_price,
                eur_value=amount_btc * eur_price,
                kyc_status=output["kyc_status"],
                kyc_fraction=Decimal(output["kyc_fraction"]),
                source_type=output["source_type"] or "unknown",
                source_label=output["source_label"] or "",
                source_evidence=_funding_evidence(
                    output["source_evidence"] or "", output["txid"], funding
                ),
                event_description=classify_event(
                    output["source_type"],
                    output["source_label"],
                    len(inputs),
                    len(all_outputs),
                ),
                mixing_event=len(parent_statuses) > 1,
                parent_utxos=parent_utxos,
                sibling_utxos=sibling_utxos,
            )
        )
        # Walk back along the largest parent input (deterministic on ties:
        # lowest input_index wins via max stability).
        current = max(parent_utxos, key=_value_sat) if parent_utxos else None

    steps.reverse()
    for i, step in enumerate(steps, 1):
        step.step_number = i
    return steps
