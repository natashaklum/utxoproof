"""Advisory engine tests (Sec. 13 rules, hand-computed)."""

import datetime
from decimal import Decimal

from utxoproof.advisory import (
    AdvisoryFlag,
    analyze_utxo,
    analyze_wallet,
    assign_flags,
    estate_score,
    is_borrow_candidate,
    portfolio_summary,
)
from utxoproof.kyc import create_sample_graph, propagate_graph, seed_source_kyc

AS_OF = datetime.date(2024, 6, 1)


def _advisory(cost: str, value: str, holding_days: int, kyc: str = "kyc", tainted: bool = False):
    as_of = datetime.date(2024, 6, 1)
    acquired = as_of - datetime.timedelta(days=holding_days)
    return analyze_utxo(
        "tx",
        0,
        Decimal("1"),
        acquired,
        Decimal(cost),
        Decimal(value),
        kyc,
        Decimal("1") if kyc == "kyc" else Decimal("0"),
        as_of,
        speculation_tainted=tainted,
    )


def test_loss_is_tax_loss_and_sell_friendly() -> None:
    adv = _advisory("10000", "8000", 100)
    assert AdvisoryFlag.TAX_LOSS_CANDIDATE in adv.flags
    assert AdvisoryFlag.SELL_FRIENDLY in adv.flags
    assert adv.tax_if_sold_eur == 0
    assert AdvisoryFlag.HOLD_RECOMMENDED not in adv.flags


def test_cheap_gain_is_sell_friendly_only() -> None:
    adv = _advisory("9500", "10000", 400)
    assert adv.flags == [AdvisoryFlag.SELL_FRIENDLY]


def test_big_seasoned_gain_collects_hold_borrow_estate() -> None:
    adv = _advisory("10000", "40000", 500)
    assert AdvisoryFlag.HOLD_RECOMMENDED in adv.flags
    assert AdvisoryFlag.BORROW_CANDIDATE in adv.flags
    assert AdvisoryFlag.ESTATE_CANDIDATE in adv.flags
    assert adv.tax_if_sold_eur == Decimal("9900")


def test_privacy_risk_and_taint() -> None:
    assert AdvisoryFlag.PRIVACY_RISK in _advisory("10000", "20000", 500, "mixed").flags
    assert AdvisoryFlag.PRIVACY_RISK in _advisory("10000", "20000", 500, "unknown").flags
    assert AdvisoryFlag.PRIVACY_RISK not in _advisory("10000", "20000", 500, "kyc").flags
    assert AdvisoryFlag.SPECULATION_TAINTED in _advisory("10000", "20000", 500, tainted=True).flags


def test_estate_score_and_borrow_boundaries() -> None:
    assert estate_score(_advisory("10000", "40000", 500)) == 6
    assert estate_score(_advisory("10000", "40000", 500, "mixed")) == 2
    assert estate_score(_advisory("10000", "11000", 100, "unknown")) == 0
    assert is_borrow_candidate(_advisory("10000", "40000", 500))
    assert not is_borrow_candidate(_advisory("9500", "10000", 400))  # 1.65% < 2%
    assert not is_borrow_candidate(_advisory("10000", "40000", 100))  # too young
    assert not is_borrow_candidate(_advisory("10000", "40000", 500, "mixed"))


CURVE = {
    datetime.date(2021, 6, 1): Decimal("5000"),
    datetime.date(2023, 1, 1): Decimal("20000"),
    datetime.date(2023, 2, 1): Decimal("25000"),
    datetime.date(2023, 3, 1): Decimal("30000"),
}


def _price(day: datetime.date) -> Decimal:
    return CURVE[day]


def _with_estate_utxo(db) -> None:
    db.execute("INSERT INTO transactions (txid, block_time) VALUES ('E', '2021-06-01')")
    db.execute(
        "INSERT INTO tx_outputs (txid, vout, value_sat, source_type) "
        "VALUES ('E', 0, 200000000, 'exchange_purchase')"
    )
    db.commit()


def test_analyze_wallet_on_sample_graph() -> None:
    db = create_sample_graph()
    _with_estate_utxo(db)
    seed_source_kyc(db)
    propagate_graph(db)
    advisories = analyze_wallet(db, _price, Decimal("40000"), AS_OF)
    by_id = {f"{a.txid}:{a.vout}": a for a in advisories}
    assert set(by_id) == {"C:1", "D:0", "E:0"}

    estate = by_id["E:0"]
    assert estate.unrealized_gain_eur == Decimal("70000")
    assert set(estate.flags) == {
        AdvisoryFlag.HOLD_RECOMMENDED,
        AdvisoryFlag.BORROW_CANDIDATE,
        AdvisoryFlag.ESTATE_CANDIDATE,
    }
    assert set(by_id["C:1"].flags) == {
        AdvisoryFlag.HOLD_RECOMMENDED,
        AdvisoryFlag.PRIVACY_RISK,
    }
    assert by_id["D:0"].flags == [AdvisoryFlag.PRIVACY_RISK]
    # Ranked by tax-if-sold descending.
    assert [a.txid for a in advisories] == ["E", "D", "C"]

    summary = portfolio_summary(advisories)
    assert summary == {
        "total_tax_eur": Decimal("30525"),
        "estate_value_eur": Decimal("80000"),
        "borrow_value_eur": Decimal("80000"),
    }


def test_assign_flags_direct() -> None:
    adv = _advisory("10000", "40000", 500)
    assert AdvisoryFlag.HOLD_RECOMMENDED in assign_flags(adv)


def test_cli_advise(tmp_path, capsys) -> None:
    import sqlite3

    from utxoproof.cli import main
    from utxoproof.kyc import create_sample_graph

    db_path = tmp_path / "adv.db"
    source = create_sample_graph()
    dest = sqlite3.connect(str(db_path))
    source.backup(dest)
    dest.close()
    assert (
        main(
            [
                "advise",
                "--db",
                str(db_path),
                "--price",
                "40000",
                "--as-of",
                "2024-06-01",
                "--out",
                str(tmp_path),
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "liquidate_tax_eur:" in out
    assert (tmp_path / "advisory.html").exists()
    html = (tmp_path / "advisory.html").read_text(encoding="utf-8")
    assert "privacy_risk" in html


def test_analyze_wallet_ignores_proven_external_outputs() -> None:
    import datetime
    from decimal import Decimal

    from utxoproof.advisory import analyze_wallet
    from utxoproof.kyc import create_sample_graph

    db = create_sample_graph()
    before = analyze_wallet(
        db, lambda _d: Decimal("20000"), Decimal("40000"), datetime.date(2024, 6, 1)
    )
    assert before, "sample graph must have unspent outputs"
    victim = f"{before[0].txid}:{before[0].vout}"
    db.execute(
        "UPDATE tx_outputs SET is_mine = 0 WHERE txid = ? AND vout = ?",
        (before[0].txid, before[0].vout),
    )
    db.commit()
    after = analyze_wallet(
        db, lambda _d: Decimal("20000"), Decimal("40000"), datetime.date(2024, 6, 1)
    )
    assert victim not in {f"{a.txid}:{a.vout}" for a in after}
    assert len(after) == len(before) - 1
