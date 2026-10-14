"""Generic tag model tests (deterministic buckets, honest channels)."""

from decimal import Decimal

from utxoproof.tags import (
    BEHAVIOR_COLORS,
    BEHAVIOR_ORDER,
    CHANNEL_COLORS,
    CHANNEL_ORDER,
    behavior_bucket,
    channel_of,
    load_tag_config,
    tags_for,
)


class _Item:
    def __init__(self, holding_days: int, tainted: bool, kyc: str) -> None:
        self.holding_days = holding_days
        self.speculation_tainted = tainted
        self.kyc_status = kyc
        self.current_value_eur = Decimal("100")


def test_behavior_priority_speculation_first() -> None:
    assert behavior_bucket(10, True) == "speculation"
    assert behavior_bucket(999, True) == "speculation"  # taint beats seasoning
    assert behavior_bucket(365, False) == "buy-and-hold"
    assert behavior_bucket(364, False) == "accumulating"
    assert behavior_bucket(0, False) == "accumulating"


def test_channel_narrow_and_asserted_wins() -> None:
    assert channel_of("exchange_purchase") == "exchange"
    assert channel_of("p2p_purchase") == "p2p"
    assert channel_of("mining") == "mining"
    assert channel_of("self_transfer") == "transfer"
    assert channel_of("") == "unknown"
    assert channel_of("whatever") == "unknown"
    assert channel_of("exchange_purchase", "bank-transfer") == "bank-transfer"
    assert channel_of("", "cash") == "cash"
    import pytest

    with pytest.raises(ValueError, match="unknown channel"):
        channel_of("", "telepathy")


def test_tags_merge_derived_mixed_and_user() -> None:
    tags = tags_for(_Item(400, False, "mixed"), "exchange_purchase", ("cold-storage",))
    assert tags == ("buy-and-hold", "exchange", "mixed", "cold-storage")
    assert tags_for(_Item(10, False, "kyc"), "") == ("accumulating", "unknown")
    # User tags never duplicate derived ones.
    assert tags_for(_Item(400, False, "kyc"), "", ("buy-and-hold",)) == (
        "buy-and-hold",
        "unknown",
    )


def test_palette_covers_every_bucket() -> None:
    assert set(BEHAVIOR_COLORS) == set(BEHAVIOR_ORDER)
    assert set(CHANNEL_COLORS) == set(CHANNEL_ORDER)


def test_load_tag_config_validates(tmp_path) -> None:
    import pytest

    cfg = tmp_path / "entities.toml"
    cfg.write_text(
        '[[entities]]\nid = "cold"\nkind = "wallet"\nlabel = "Cold"\n'
        '[entity_tags]\ncold = ["bank-transfer", "cold-storage"]\n'
        '[utxo_tags]\n"abc:0" = ["cash"]\n',
        encoding="utf-8",
    )
    entity_tags, utxo_tags = load_tag_config(cfg)
    assert entity_tags == {"cold": ("bank-transfer", "cold-storage")}
    assert utxo_tags == {"abc:0": ("cash",)}

    cfg.write_text('[[entities]]\nid = "cold"\n[entity_tags]\nghost = ["x"]\n', encoding="utf-8")
    with pytest.raises(ValueError, match="unknown entity"):
        load_tag_config(cfg)

    cfg.write_text('[[entities]]\nid = "cold"\n[utxo_tags]\n"nope" = ["x"]\n', encoding="utf-8")
    with pytest.raises(ValueError, match="txid:vout"):
        load_tag_config(cfg)
