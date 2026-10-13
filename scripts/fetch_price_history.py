"""Fetch daily price history into data/ (one-off + refresh).

Sources (all free, no keys):
- BTC/EUR 2024-10 -> now: Kraken OHLC directly (native EUR daily close).
- BTC/USD 2010 -> 2024: blockchain.info charts (daily), converted with ECB
  (covers the months before Kraken history starts; Kraken wins overlaps).
- USD/EUR 1999 -> now: ECB eurofxref-hist.zip (business days; forward-filled).

Outputs: data/btc_eur_daily.csv (date,close_eur,source),
data/btc_usd_daily.csv (date,close_usd,source, raw upstream BTC/USD),
data/usd_eur_daily.csv (date,usd_per_eur). Deterministic: re-running with the
same upstream data yields identical files. The raw USD + ECB files let later
tweaks re-stitch data/btc_eur_daily.csv offline (--offline) without touching
API rate limits.
"""

from __future__ import annotations

import csv
import datetime
import io
import zipfile
from decimal import Decimal
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "utxoproof" / "data"

BCI_URL = "https://api.blockchain.info/charts/market-price"
ECB_ZIP_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist.zip"
KRAKEN_URL = "https://api.kraken.com/0/public/OHLC"


def fetch_bci_usd(client: httpx.Client, start: str, timespan: str) -> dict[str, Decimal]:
    response = client.get(BCI_URL, params={"timespan": timespan, "start": start, "format": "json"})
    response.raise_for_status()
    out = {}
    for point in response.json()["values"]:
        day = datetime.datetime.fromtimestamp(point["x"], datetime.UTC).date()
        out[day.isoformat()] = Decimal(str(point["y"]))
    return out


def fetch_kraken_eur(client: httpx.Client) -> dict[str, Decimal]:
    out: dict[str, Decimal] = {}
    since: int | None = None
    while True:
        params: dict[str, str | int] = {"pair": "XBTEUR", "interval": 1440}
        if since is not None:
            params["since"] = since
        response = client.get(KRAKEN_URL, params=params)
        response.raise_for_status()
        payload = response.json()
        if payload.get("error"):
            raise RuntimeError(f"Kraken error: {payload['error']}")
        candles = next(iter([v for k, v in payload["result"].items() if k != "last"]))
        for candle in candles:
            day = datetime.datetime.fromtimestamp(candle[0], datetime.UTC).date()
            out[day.isoformat()] = Decimal(str(candle[4]))
        new_since = int(payload["result"]["last"])
        if new_since == since or len(candles) < 720:
            break
        since = new_since
    return out


def fetch_ecb_usd(client: httpx.Client) -> dict[str, Decimal]:
    response = client.get(ECB_ZIP_URL)
    response.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
        name = next(n for n in zf.namelist() if n.endswith(".csv"))
        text = zf.read(name).decode("utf-8")
    rows = list(csv.DictReader(io.StringIO(text)))
    usd_col = next(c for c in rows[0].keys() if c and c.strip().upper() == "USD")
    out = {}
    for row in rows:
        date = (row.get("Date") or "").strip()
        rate = (row.get(usd_col) or "").strip()
        if date and rate and rate != "N/A":
            out[date] = Decimal(rate)
    return out


def forward_fill(rates: dict[str, Decimal], start: str, end: str) -> dict[str, Decimal]:
    """Fill weekends/holidays with the most recent prior rate."""
    day = datetime.date.fromisoformat(start)
    last_day = datetime.date.fromisoformat(end)
    filled: dict[str, Decimal] = {}
    current: Decimal | None = None
    while day <= last_day:
        key = day.isoformat()
        if key in rates:
            current = rates[key]
        if current is not None:
            filled[key] = current
        day += datetime.timedelta(days=1)
    return filled


def stitch_btc_eur(
    early_usd: dict[str, Decimal],
    kraken: dict[str, Decimal],
    ecb: dict[str, Decimal],
) -> dict[str, tuple[str, str]]:
    """Merge raw inputs into date -> (close_eur, source). Pure (offline-testable).

    Kraken native EUR wins overlaps; pre-market zero USD prints carry no
    signal and are skipped. Raises listing the first gaps instead of shipping
    a holey series.
    """
    if not early_usd or not ecb:
        raise ValueError("stitch needs BTC/USD and ECB inputs")
    end = max(kraken) if kraken else max(early_usd)
    filled = forward_fill(ecb, min(early_usd), end)
    eur: dict[str, tuple[str, str]] = {}  # date -> (close_eur, source)
    for date, usd_price in sorted(early_usd.items()):
        if date in kraken or usd_price <= 0:
            continue  # pre-market zeros carry no signal; Kraken wins overlaps
        eur[date] = (str(usd_price / filled[date]), "blockchain.info-x-ecb")
    for date in sorted(kraken):
        eur[date] = (str(kraken[date]), "kraken")
    assert_gapless(sorted(eur))
    return eur


def assert_gapless(dates: list[str]) -> None:
    """A holey price series must fail the regen loudly, never ship silently."""
    if not dates:
        raise ValueError("no price dates to check")
    days = [datetime.date.fromisoformat(d) for d in dates]
    have = set(days)
    missing = [
        (days[0] + datetime.timedelta(days=n)).isoformat()
        for n in range((days[-1] - days[0]).days + 1)
        if (days[0] + datetime.timedelta(days=n)) not in have
    ]
    if missing:
        shown = ", ".join(missing[:10])
        noun = "gap day" if len(missing) == 1 else "gap days"
        raise ValueError(f"price series has {len(missing)} {noun} (first: {shown})")


def _read_rate_file(path: Path) -> dict[str, Decimal]:
    with open(path, newline="", encoding="utf-8") as f:
        return {
            (row.get("date") or "").strip(): Decimal(
                str(row.get("close_usd") or row.get("usd_per_eur"))
            )
            for row in csv.DictReader(f)
            if (row.get("date") or "").strip()
        }


def _overlay_existing_rates(path: Path, rates: dict[str, Decimal]) -> None:
    """Merge a previously shipped raw file in place: existing rows win.

    Price prints are immutable history; a regen must only add dates, never
    revise them (tax basis already filed off old rows must stay reproducible).
    """
    if not path.is_file():
        return
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            date = (row.get("date") or "").strip()
            value = row.get("close_usd") or row.get("usd_per_eur")
            if date and value:
                rates[date] = Decimal(str(value))


def _overlay_existing_eur(path: Path, eur: dict[str, tuple[str, str]]) -> None:
    if not path.is_file():
        return
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            date = (row.get("date") or "").strip()
            if date:
                eur[date] = (str(row.get("close_eur")), str(row.get("source")))


def _write_btc_eur(data_dir: Path, eur: dict[str, tuple[str, str]]) -> Path:
    btc_path = data_dir / "btc_eur_daily.csv"
    with open(btc_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["date", "close_eur", "source"])
        for date in sorted(eur):
            close, source = eur[date]
            writer.writerow([date, close, source])
    print(f"  {len(eur)} days ({min(eur)} -> {max(eur)}) -> {btc_path}")
    return btc_path


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Fetch / re-stitch daily price history")
    parser.add_argument("--data-dir", default=str(DATA), help="Output directory")
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Re-stitch btc_eur_daily.csv from the raw files on disk (no network)",
    )
    args = parser.parse_args()
    data_dir = Path(args.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)

    if args.offline:
        print("Re-stitching from raw files (offline) ...")
        offline_usd = _read_rate_file(data_dir / "btc_usd_daily.csv")
        offline_ecb = _read_rate_file(data_dir / "usd_eur_daily.csv")
        kraken: dict[str, Decimal] = {}
        with open(data_dir / "btc_eur_daily.csv", newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row.get("source") == "kraken":
                    kraken[(row.get("date") or "").strip()] = Decimal(str(row["close_eur"]))
        eur = stitch_btc_eur(offline_usd, kraken, offline_ecb)
        _write_btc_eur(data_dir, eur)
        return 0

    client = httpx.Client(timeout=60.0)

    print("ECB USD/EUR ...")
    ecb = fetch_ecb_usd(client)
    _overlay_existing_rates(data_dir / "usd_eur_daily.csv", ecb)
    usd_path = data_dir / "usd_eur_daily.csv"
    with open(usd_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["date", "usd_per_eur"])
        for date in sorted(ecb):
            writer.writerow([date, str(ecb[date])])
    print(f"  {len(ecb)} business days -> {usd_path}")

    print("blockchain.info BTC/USD 2010-2014 ...")
    early_usd: dict[str, Decimal] = {}
    for year in ("2010", "2011", "2012", "2013"):
        early_usd.update(fetch_bci_usd(client, f"{year}-01-01", "1year"))
    print("blockchain.info BTC/USD 2014-2019, 2019-2024, 2024 seam ...")
    early_usd.update(fetch_bci_usd(client, "2014-01-01", "5years"))
    early_usd.update(fetch_bci_usd(client, "2019-01-01", "5years"))
    early_usd.update(fetch_bci_usd(client, "2024-01-01", "1year"))
    print(f"  {len(early_usd)} USD days")
    _overlay_existing_rates(data_dir / "btc_usd_daily.csv", early_usd)
    btc_usd_path = data_dir / "btc_usd_daily.csv"
    with open(btc_usd_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["date", "close_usd", "source"])
        for date in sorted(early_usd):
            writer.writerow([date, str(early_usd[date]), "blockchain.info"])
    print(f"  raw upstream BTC/USD kept -> {btc_usd_path}")

    print("Kraken BTC/EUR ...")
    kraken = fetch_kraken_eur(client)
    if not kraken:
        raise RuntimeError("Kraken returned no candles; refusing to ship a BCI-only file")
    print(f"  {len(kraken)} Kraken days ({min(kraken)} -> {max(kraken)})")

    eur = stitch_btc_eur(early_usd, kraken, ecb)
    _overlay_existing_eur(data_dir / "btc_eur_daily.csv", eur)
    assert_gapless(sorted(eur))
    _write_btc_eur(data_dir, eur)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
