"""Sprint 1: EUR price oracle (mocked HTTP, no network in tests)."""

import datetime
import sqlite3
from decimal import Decimal

import httpx

from utxoproof.db import open_memory_db
from utxoproof.price_oracle import EURPriceOracle


def _kraken_candle(date: datetime.date, close: str) -> dict:
    ts = int(datetime.datetime(date.year, date.month, date.day, tzinfo=datetime.UTC).timestamp())
    return {"error": [], "result": {"XXBTZEUR": [[ts, "1", "1", "1", close, "1", "1", 0]]}}


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _oracle(handler) -> tuple[EURPriceOracle, sqlite3.Connection, dict[str, int]]:
    calls: dict[str, int] = {"n": 0}
    base_handler = handler

    def counting(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return base_handler(request)

    db = open_memory_db()
    return EURPriceOracle(db, _client(counting)), db, calls


def test_kraken_close_and_cache() -> None:
    day = datetime.date(2023, 1, 10)

    def handler(request: httpx.Request) -> httpx.Response:
        assert "OHLC" in request.url.path
        return httpx.Response(200, json=_kraken_candle(day, "20000.5"))

    oracle, _, calls = _oracle(handler)
    assert oracle.get_btc_eur(day) == Decimal("20000.5")
    assert oracle.get_btc_eur(day) == Decimal("20000.5")
    assert calls["n"] == 1  # second call served from SQLite cache


def test_coingecko_fallback_on_kraken_error() -> None:
    from decimal import Decimal

    day = datetime.date(2023, 1, 10)

    def handler(request: httpx.Request) -> httpx.Response:
        if "OHLC" in request.url.path:
            return httpx.Response(200, json={"error": ["EGeneral:bad"], "result": {}})
        return httpx.Response(200, json={"market_data": {"current_price": {"eur": 19950.25}}})

    oracle, db, _ = _oracle(handler)
    assert oracle.get_btc_eur(day) == Decimal("19950.25")
    row = db.execute("SELECT source FROM price_cache WHERE pair='BTC/EUR'").fetchone()
    assert row == ("coingecko",)


def test_ecb_weekend_carries_friday_rate() -> None:
    from decimal import Decimal

    saturday = datetime.date(2023, 1, 14)  # a Saturday

    def handler(request: httpx.Request) -> httpx.Response:
        assert "ecb.europa.eu" in str(request.url)
        return httpx.Response(
            200,
            text="TIME_PERIOD,OBS_VALUE\n2023-01-13,1.0800\n2023-01-12,1.0750\n",
        )

    oracle, _, _ = _oracle(handler)
    # 1/1.08 USD->EUR factor from Friday's publication.
    assert oracle.get_fiat_eur("USD", saturday) == Decimal("1") / Decimal("1.0800")


def test_csv_seed_serves_offline_and_never_clobbers(tmp_path) -> None:
    from utxoproof.price_oracle import EURPriceOracle

    csv_path = tmp_path / "seed.csv"
    csv_path.write_text(
        "date,close_eur,source\n2021-11-10,57928.80,seed\n2023-01-01,15494.82,seed\n",
        encoding="utf-8",
    )

    def exploding(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no network expected")

    db = open_memory_db()
    oracle = EURPriceOracle(db, _client(exploding))
    assert oracle.load_csv(csv_path, "BTC/EUR", "seed") == 2
    assert oracle.get_btc_eur(datetime.date(2021, 11, 10)) == Decimal("57928.80")
    # Reloading is a no-op; existing (live) rows win.
    assert oracle.load_csv(csv_path, "BTC/EUR", "seed") == 0

    usd_path = tmp_path / "usd.csv"
    usd_path.write_text("date,usd_per_eur\n2023-01-13,1.0800\n", encoding="utf-8")
    assert oracle.load_csv(usd_path, "USD/EUR", "ecb-archive", invert=True) == 1
    assert oracle.get_fiat_eur("USD", datetime.date(2023, 1, 13)) == Decimal("1") / Decimal(
        "1.0800"
    )


def _bundled_last_date() -> datetime.date:
    from utxoproof.price_oracle import bundled_history_path

    with open(bundled_history_path(), newline="", encoding="utf-8") as f:
        import csv

        rows = [r for r in csv.DictReader(f) if r.get("date")]
    return datetime.date.fromisoformat(rows[-1]["date"][:10])


def test_bundled_price_at_exact_date_offline() -> None:
    from utxoproof.cli import _bundled_price_at
    from utxoproof.db import open_memory_db

    price, note = _bundled_price_at(open_memory_db(), datetime.date(2023, 1, 10))
    assert price > 0
    assert note == "bundled history close 2023-01-10"


def test_bundled_price_at_beyond_coverage_is_stale_not_invented() -> None:
    from utxoproof.cli import _bundled_price_at
    from utxoproof.db import open_memory_db

    last = _bundled_last_date()
    price, note = _bundled_price_at(open_memory_db(), last + datetime.timedelta(days=9))
    assert price > 0
    assert f"bundled history close {last.isoformat()}" in note
    assert "9 days stale" in note


def test_bundled_curve_strict_outside_coverage() -> None:
    import pytest

    from utxoproof.cli import _bundled_curve
    from utxoproof.db import open_memory_db

    curve = _bundled_curve(open_memory_db())
    assert curve(datetime.date(2023, 1, 10)) > 0
    with pytest.raises(ValueError, match="2009-01-03"):
        curve(datetime.date(2009, 1, 3))


def test_load_price_history_strict_without_network(monkeypatch) -> None:
    import httpx
    import pytest

    from utxoproof.cli import load_price_history

    class NoNetworkClient:
        def __init__(self, *args, **kwargs):
            pass

        def __getattr__(self, name: str):
            raise AssertionError("network used on the default price path")

    monkeypatch.setattr(httpx, "Client", NoNetworkClient)
    price_at = load_price_history(None)
    assert price_at is not None
    assert price_at(datetime.date(2023, 1, 10)) > 0
    with pytest.raises(ValueError, match="2009-01-03"):
        price_at(datetime.date(2009, 1, 3))


def test_status_without_price_is_offline_and_bundled(tmp_path, capsys, monkeypatch) -> None:
    from pathlib import Path

    import httpx

    from utxoproof.cli import main

    class NoNetworkClient:
        def __init__(self, *args, **kwargs):
            pass

        def __getattr__(self, name: str):
            raise AssertionError("network used on the default price path")

    monkeypatch.setattr(httpx, "Client", NoNetworkClient)
    assert (
        main(
            [
                "--data-dir",
                str(tmp_path),
                "status",
                "--input",
                str(Path(__file__).parent / "fixtures" / "manual_2023.csv"),
                "--db",
                str(tmp_path / "t.db"),
                "--out",
                str(tmp_path / "status"),
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "bundled history close" in out
    assert (tmp_path / "status" / "status.html").is_file()


def test_live_prices_opt_in_still_queries_oracle(tmp_path, capsys, monkeypatch) -> None:
    from pathlib import Path

    import httpx

    from utxoproof.cli import main

    day = datetime.datetime.now(datetime.UTC).date() - datetime.timedelta(days=1)
    ts = int(datetime.datetime(day.year, day.month, day.day, tzinfo=datetime.UTC).timestamp())

    def handler(request: httpx.Request) -> httpx.Response:
        assert "OHLC" in request.url.path
        return httpx.Response(
            200,
            json={"error": [], "result": {"XXBTZEUR": [[ts, "1", "1", "1", "77777", "1", "1", 0]]}},
        )

    real_client = httpx.Client
    monkeypatch.setattr(
        httpx, "Client", lambda *a, **k: real_client(transport=httpx.MockTransport(handler))
    )
    assert (
        main(
            [
                "--data-dir",
                str(tmp_path),
                "status",
                "--input",
                str(Path(__file__).parent / "fixtures" / "manual_2023.csv"),
                "--db",
                str(tmp_path / "t.db"),
                "--live-prices",
            ]
        )
        == 0
    )
    assert "Kraken close" in capsys.readouterr().out
