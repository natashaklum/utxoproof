"""Generic multi-tag model for UTXOs and bank records (tax/privacy/channel axes).

Tags are deterministic and explainable: every tag states *why* it applies, and
derived tags render distinctly from user assertions so nobody mistakes a
measurement for a guess. Three exclusive bucket axes (each sums to 100%, one
donut apiece) plus an overlapping free-tag set per item:

- behavior: speculation (tainted) > buy-and-hold (>= ``HOLD_DAYS``) > accumulating
- channel: user-asserted > source-derived (exchange/p2p/mining/transfer/unknown)
- privacy: an explicit ``mixed`` tag where it applies, ``privacy-risk`` where
  cleanliness cannot be proven (mixed or unknown status)
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Protocol


class Tagged(Protocol):
    """Structural minimum for tag derivation (UTXOAdvisory satisfies it)."""

    holding_days: int
    speculation_tainted: bool
    kyc_status: str
    current_value_eur: Decimal


HOLD_DAYS = 365

BEHAVIOR_ORDER = ("speculation", "buy-and-hold", "accumulating")
BEHAVIOR_COLORS = {
    "speculation": "#8e5bd6",
    "buy-and-hold": "#0e9f8a",
    "accumulating": "#e8a13d",
}

CHANNEL_ORDER = (
    "bank-transfer",
    "cash",
    "card",
    "exchange",
    "p2p",
    "mining",
    "transfer",
    "unknown",
)
CHANNEL_COLORS = {
    "bank-transfer": "#2f6fed",
    "cash": "#189a52",
    "card": "#8e5bd6",
    "exchange": "#e8a13d",
    "p2p": "#d35400",
    "mining": "#7f8c8d",
    "transfer": "#5dade2",
    "unknown": "#b0b0b0",
}

# Chain acquisition source -> funding channel. Deliberately narrow: the chain
# only proves *where coins arrived from*, never how fiat entered the system
# (bank vs card lives on trades/bank rows with evidence, or in user tags).
SOURCE_CHANNELS = {
    "exchange_purchase": "exchange",
    "p2p_purchase": "p2p",
    "mining": "mining",
    "self_transfer": "transfer",
    "received": "transfer",
}


def behavior_bucket(holding_days: int, speculation_tainted: bool) -> str:
    """Exclusive tax-behavior bucket (priority order = tuple order)."""
    if speculation_tainted:
        return "speculation"
    if holding_days >= HOLD_DAYS:
        return "buy-and-hold"
    return "accumulating"


def channel_of(source_type: str, user_channel: str = "") -> str:
    """Exclusive funding-channel bucket; an asserted channel always wins."""
    if user_channel:
        if user_channel not in CHANNEL_ORDER:
            raise ValueError(
                f"unknown channel tag {user_channel!r}; expected one of {CHANNEL_ORDER}"
            )
        return user_channel
    return SOURCE_CHANNELS.get(source_type or "", "unknown")


def tags_for(
    item: Tagged,
    source_type: str = "",
    user_tags: tuple[str, ...] = (),
    user_channel: str = "",
) -> tuple[str, ...]:
    """Overlapping generic tag set: behavior + channel + privacy + user tags."""
    tags = [behavior_bucket(item.holding_days, item.speculation_tainted)]
    tags.append(channel_of(source_type, user_channel))
    if item.kyc_status == "mixed":
        tags.append("mixed")
    if item.kyc_status in ("mixed", "unknown"):
        tags.append("privacy-risk")
    seen = set(tags)
    for tag in user_tags:
        if tag not in seen:
            tags.append(tag)
            seen.add(tag)
    return tuple(tags)


TagMaps = tuple[dict[str, tuple[str, ...]], dict[str, tuple[str, ...]]]


def load_tag_config(path: str | Path) -> TagMaps:
    """``[entity_tags]`` / ``[utxo_tags]`` tables from a TOML file.

    Returns ``(entity_tags, utxo_tags)`` mapping ids / ``txid:vout`` refs to
    tag tuples. Unknown entities abort (consistent with ``[[fiat]]``
    validation); malformed UTXO refs abort too.
    """
    import tomllib

    with open(Path(path), "rb") as f:
        data = tomllib.load(f)

    from utxoproof.portfolio import load_entities  # validates [[entities]] presence

    entities, _ = load_entities(path)
    known = {e.id for e in entities}
    entity_tags: dict[str, tuple[str, ...]] = {}
    for eid, tags in (data.get("entity_tags") or {}).items():
        if eid not in known:
            raise ValueError(f"entity_tags for unknown entity {eid!r} in {path}")
        entity_tags[str(eid)] = tuple(str(t) for t in tags)
    utxo_tags: dict[str, tuple[str, ...]] = {}
    for ref, tags in (data.get("utxo_tags") or {}).items():
        if ":" not in str(ref):
            raise ValueError(f"utxo_tags ref {ref!r} must look like txid:vout in {path}")
        utxo_tags[str(ref)] = tuple(str(t) for t in tags)
    return entity_tags, utxo_tags
