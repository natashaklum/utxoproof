"""Portfolio overview model + dependency-free SVG charts (dashboard).

Entities (wallets, bank accounts, exchange balances) hold UTXO or fiat
positions. Pure functions over plain data so tests stay offline; the demo
site wires them to the sample graph. Charts are inline SVG (no JavaScript,
print-friendly).
"""

from __future__ import annotations

import csv
import datetime
import html
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import ClassVar

from utxoproof.advisory import UTXOAdvisory


@dataclass
class Entity:
    id: str
    kind: str  # wallet | bank | exchange
    label: str
    detail: str = ""
    wallet: str = ""  # synced on-chain wallet whose UTXOs belong here


@dataclass
class UtxoHolding:
    entity_id: str
    txid: str
    vout: int
    btc: Decimal
    eur_value: Decimal
    kyc_status: str
    cost_eur: Decimal = Decimal("0")
    address: str = ""


@dataclass
class FiatHolding:
    entity_id: str
    amount_eur: Decimal
    note: str = ""


@dataclass
class Portfolio:
    entities: list[Entity] = field(default_factory=list)
    utxos: list[UtxoHolding] = field(default_factory=list)
    fiat: list[FiatHolding] = field(default_factory=list)
    current_price_eur: Decimal = Decimal("0")

    # Canonical status order: stable colors and rows everywhere (value ties
    # must not reshuffle which status gets which color).
    STATUS_ORDER: ClassVar[tuple[str, ...]] = ("kyc", "non_kyc", "mixed", "unknown")
    STATUS_COLORS: ClassVar[dict[str, str]] = {
        "kyc": "#2f6fed",
        "non_kyc": "#3faf6e",
        "mixed": "#e8a13d",
        "unknown": "#b0b0b0",
    }

    @property
    def btc_total(self) -> Decimal:
        return sum((u.btc for u in self.utxos), Decimal("0"))

    @property
    def btc_value_eur(self) -> Decimal:
        return sum((u.eur_value for u in self.utxos), Decimal("0"))

    @property
    def fiat_total_eur(self) -> Decimal:
        return sum((f.amount_eur for f in self.fiat), Decimal("0"))

    @property
    def net_worth_eur(self) -> Decimal:
        return self.btc_value_eur + self.fiat_total_eur

    @property
    def cost_basis_eur(self) -> Decimal:
        return sum((u.cost_eur for u in self.utxos), Decimal("0"))

    @property
    def unrealized_eur(self) -> Decimal:
        return self.btc_value_eur - self.cost_basis_eur

    def entity_value_eur(self, entity_id: str) -> Decimal:
        utxo_value = sum(
            (u.eur_value for u in self.utxos if u.entity_id == entity_id), Decimal("0")
        )
        fiat_value = sum(
            (f.amount_eur for f in self.fiat if f.entity_id == entity_id), Decimal("0")
        )
        return utxo_value + fiat_value

    def dominant_kyc(self, entity_id: str) -> str | None:
        """KYC status holding the largest BTC value in an entity (None if none)."""
        by_status: dict[str, Decimal] = {}
        for u in self.utxos:
            if u.entity_id == entity_id:
                by_status[u.kyc_status] = by_status.get(u.kyc_status, Decimal("0")) + u.eur_value
        if not by_status:
            return None
        return max(sorted(by_status), key=lambda s: by_status[s])

    def allocation(self) -> list[tuple[str, str, Decimal, Decimal]]:
        """(entity_id, label, value_eur, share_pct) sorted by value desc."""
        total = self.net_worth_eur
        rows = [(e.id, e.label, self.entity_value_eur(e.id)) for e in self.entities]
        rows.sort(key=lambda r: r[2], reverse=True)
        return [
            (eid, label, value, (value / total * 100 if total > 0 else Decimal("0")))
            for eid, label, value in rows
        ]

    def kyc_split(self) -> list[tuple[str, Decimal]]:
        """BTC value per KYC status in canonical status order."""
        by_status: dict[str, Decimal] = {}
        for u in self.utxos:
            by_status[u.kyc_status] = by_status.get(u.kyc_status, Decimal("0")) + u.eur_value
        return [(s, by_status[s]) for s in self.STATUS_ORDER if s in by_status]


def _esc(text: str) -> str:
    return html.escape(text)


def svg_bars(
    items: list[tuple[str, Decimal, str]],
    width: int = 560,
    row_height: int = 26,
    color: str = "#2f6fed",
    colors: dict[str, str] | None = None,
) -> str:
    """Horizontal bar chart. Items: (label, value, link-href or "").

    One ``color`` for all bars, or per-label ``colors`` keyed by the label
    prefix before " (" (entity bars use their dominant-KYC color)."""
    peak = max([value for _, value, _ in items] + [Decimal("0")])
    height = max(row_height * len(items) + 10, 30)
    rows = []
    for i, (label, value, href) in enumerate(items):
        y = 5 + i * row_height
        bar_w = int(width * 0.52 * float(value / peak)) if peak > 0 else 0
        label_cell = f'<a href="{_esc(href)}">{_esc(label)}</a>' if href else _esc(label)
        bar_color = (colors or {}).get(label.split(" (")[0], color)
        bar_cell = f'<rect x="150" y="{y}" width="{bar_w}" height="16" fill="{bar_color}">'
        if href:
            bar_cell = f'<a href="{_esc(href)}">{bar_cell}</a>'
        rows.append(
            f'<text x="0" y="{y + 13}" font-size="12">{label_cell}</text>'
            f"{bar_cell}"
            f'<text x="{160 + int(width * 0.52)}" y="{y + 13}" font-size="12">{value:,.2f}</text>'
        )
    return f'<svg width="{width}" height="{height}" role="img">' + "".join(rows) + "</svg>"


def donut_legend(
    segments: list[tuple[str, Decimal]],
) -> list[tuple[str, str, Decimal]]:
    """(color, label, value) legend rows matching the donut ring colors."""
    return [
        (Portfolio.STATUS_COLORS.get(label, "#8e5bd6"), label, value) for label, value in segments
    ]


def donut_ring(
    segments: list[tuple[str, Decimal]],
    size: int = 180,
    thickness: int = 26,
) -> str:
    """Donut ring as a conic-gradient div (no fragile 180-degree arc math).

    Center shows the total and the largest segment's share. Legend is
    rendered as HTML by the caller (see ``donut_legend``).
    """
    total = sum((v for _, v in segments), Decimal("0"))
    stops: list[str] = []
    pos = 0.0
    for label, value in segments:
        frac = float(value / total) if total > 0 else 0.0
        color = Portfolio.STATUS_COLORS.get(label, "#8e5bd6")
        stops.append(f"{color} {pos:.1f}% {pos + frac * 100:.1f}%")
        pos += frac * 100
    leader = max([float(v / total) if total > 0 else 0.0 for _, v in segments] + [0.0])
    hole = (size - thickness * 2) // 2
    return (
        f'<div class="donut" style="width:{size}px;height:{size}px;'
        "background:conic-gradient(" + ", ".join(stops) + ')">'
        f'<div class="donut-hole" style="width:{hole * 2}px;height:{hole * 2}px;">'
        f'<div class="donut-total">{total:,.0f}</div>'
        f'<div class="donut-share">{leader:.0%}</div>'
        "</div></div>"
    )


def svg_sparkline(
    points: list[tuple[str, Decimal]],
    width: int = 560,
    height: int = 120,
    label: str = "",
) -> str:
    """Minimal value-history sparkline. Points: (date-iso, value)."""
    if not points:
        return ""
    values = [float(v) for _, v in points]
    lo, hi = min(values), max(values)
    span = hi - lo if hi > lo else 1.0
    n = len(points)
    coords = [
        (
            40 + (width - 60) * (i / (n - 1) if n > 1 else 0.5),
            height - 20 - (height - 40) * ((v - lo) / span),
        )
        for i, v in enumerate(values)
    ]
    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in coords)
    first_date = points[0][0]
    last_date = points[-1][0]
    return (
        f'<svg width="{width}" height="{height}" role="img">'
        f'<polyline points="{line}" fill="none" stroke="#2f6fed" stroke-width="2"/>'
        f'<text x="40" y="{height - 5}" font-size="11">{_esc(first_date)}</text>'
        f'<text x="{width - 5}" y="{height - 5}" font-size="11"'
        f' text-anchor="end">{_esc(last_date)}</text>'
        f'<text x="5" y="15" font-size="11">High {hi:,.0f}</text>'
        f'<text x="5" y="{height - 20}" font-size="11">Low {lo:,.0f}</text>'
        + (f"<title>{_esc(label)}</title>" if label else "")
        + "</svg>"
    )


def load_price_series(
    csv_path: str | Path, days: int = 365, max_points: int = 120
) -> list[tuple[str, Decimal]]:
    """Last ``days`` of a (date, close, ...) price CSV, downsampled."""
    with open(Path(csv_path), newline="", encoding="utf-8") as f:
        rows = [
            (row["date"][:10], Decimal(row["close_eur"]))
            for row in csv.DictReader(f)
            if row.get("date") and row.get("close_eur")
        ]
    rows.sort()
    window = rows[-days:]
    step = max(1, len(window) // max_points)
    return window[::step]


def load_entities(path: str | Path) -> tuple[list[Entity], list[FiatHolding]]:
    """Parse an entities TOML file (see ``examples/entities.example.toml``).

    ``[[entities]]`` entries need ``id`` (``kind``/``label``/``detail``/``wallet``
    optional; ``wallet`` names the synced on-chain wallet whose UTXOs belong to
    the entity, default ``utxoproof_watchonly``). ``[[fiat]]`` entries attach a
    euro balance to an entity by ``entity_id``.
    """
    import tomllib

    with open(Path(path), "rb") as f:
        data = tomllib.load(f)
    entities = [
        Entity(
            id=str(e["id"]),
            kind=str(e.get("kind", "wallet")),
            label=str(e.get("label", e["id"])),
            detail=str(e.get("detail", "")),
            wallet=str(e.get("wallet", "")),
        )
        for e in data.get("entities", [])
    ]
    if not entities:
        raise ValueError(f"no [[entities]] in {path}")
    fiat = [
        FiatHolding(
            entity_id=str(f["entity_id"]),
            amount_eur=Decimal(str(f["amount_eur"])),
            note=str(f.get("note", "")),
        )
        for f in data.get("fiat", [])
    ]
    known = {e.id for e in entities}
    for holding in fiat:
        if holding.entity_id not in known:
            raise ValueError(f"fiat entry for unknown entity {holding.entity_id!r} in {path}")
    return entities, fiat


def portfolio_from_db(
    db: sqlite3.Connection,
    entities: list[Entity],
    fiat: list[FiatHolding],
    wallet: str,
    price_at: Callable[[datetime.date], Decimal],
    current_price_eur: Decimal,
    as_of: datetime.date,
) -> tuple[Portfolio, list[UTXOAdvisory]]:
    """Build a ``Portfolio`` from a synced on-chain DB plus entity config.

    Every unspent output lands in the entity whose ``wallet`` matches the
    synced wallet (falling back to the single entity when only one is
    declared). Returns ``(portfolio, advisories)`` — the advisories carry
    per-UTXO cost basis, value, KYC status and flags for entity pages.
    """
    from utxoproof.advisory import analyze_wallet
    from utxoproof.kyc import propagate_graph, seed_source_kyc

    seed_source_kyc(db)
    propagate_graph(db)
    advisories = analyze_wallet(db, price_at, current_price_eur, as_of)
    named = [e for e in entities if e.wallet == wallet]
    if named:
        owner_id = named[0].id
    elif len(entities) == 1:
        owner_id = entities[0].id
    else:
        defaulted = [e for e in entities if not e.wallet]
        owner_id = defaulted[0].id if defaulted else entities[0].id
    utxos = [
        UtxoHolding(
            owner_id,
            a.txid,
            a.vout,
            a.amount_btc,
            a.current_value_eur,
            a.kyc_status,
            a.acquisition_cost_eur,
            a.address,
        )
        for a in advisories
    ]
    return (
        Portfolio(
            entities=entities,
            utxos=utxos,
            fiat=list(fiat),
            current_price_eur=current_price_eur,
        ),
        advisories,
    )
