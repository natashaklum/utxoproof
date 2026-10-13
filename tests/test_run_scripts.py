"""End-to-end runs of examples/run.sh (offline fixtures).

Two modes:
  MODE=prepare   (default in the script)   — stops if check reports shortfalls
  MODE=refresh   — idempotent rerun; shortfalls print but do not stop
"""

import os
import stat
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FIXTURES = REPO / "tests" / "fixtures"


def _run(script: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    merged = dict(os.environ)
    merged.update(env)
    merged["UTXOPROOF_BIN"] = os.path.join(
        os.path.dirname(__import__("sys").executable), "utxoproof"
    )
    # S603/S607: fixed argv (bash + repo-relative script under test); no user input.
    return subprocess.run(  # noqa: S603
        ["bash", str(REPO / "examples" / script)],  # noqa: S607
        capture_output=True,
        text=True,
        env=merged,
        timeout=300,
    )


def _shortfall_ledgers(path: Path) -> None:
    path.write_text(
        "txid,refid,time,type,subtype,aclass,subclass,asset,wallet,amount,fee,balance\n"
        "L1,Q1,2023-01-01 00:00:00,trade,,currency,crypto,XXBT,spot / main,"
        "-0.5,0.0,0.0\n"
        "L2,Q1,2023-01-01 00:00:00,trade,,currency,fiat,ZEUR,spot / main,"
        "12500.00,5.00,12500.00\n"
    )


def test_refresh_end_to_end(tmp_path: Path) -> None:
    data = tmp_path / "data"
    proc = _run(
        "run.sh",
        {
            "DATA_DIR": str(data),
            "KRAKEN_LEDGERS": str(FIXTURES / "kraken_ledgers_2023.csv"),
            "KRAKEN_TRADES": "",
            "TAX_YEAR": "2023",
            "BTC_PRICE": "40000",
            "MODE": "refresh",
        },
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert (data / "manual.csv").is_file()
    assert (data / "2023" / "report.html").is_file()
    assert list((data / "2023").glob("utxoproof_evidence_2023_*.zip"))
    assert (data / "alltime" / "summary.html").is_file()


def test_prepare_aborts_on_shortfalls(tmp_path: Path) -> None:
    ledgers = tmp_path / "ledgers.csv"
    _shortfall_ledgers(ledgers)
    proc = _run(
        "run.sh",
        {
            "DATA_DIR": str(tmp_path / "data"),
            "KRAKEN_LEDGERS": str(ledgers),
            "KRAKEN_TRADES": "",
            "TAX_YEAR": "2023",
            "BTC_PRICE": "40000",
            "MODE": "prepare",
        },
    )
    assert proc.returncode != 0
    assert "shortfalls" in proc.stdout + proc.stderr


def test_prepare_allow_shortfalls_proceeds_with_warning(tmp_path: Path) -> None:
    ledgers = tmp_path / "ledgers.csv"
    _shortfall_ledgers(ledgers)
    proc = _run(
        "run.sh",
        {
            "DATA_DIR": str(tmp_path / "data"),
            "KRAKEN_LEDGERS": str(ledgers),
            "KRAKEN_TRADES": "",
            "TAX_YEAR": "2023",
            "BTC_PRICE": "40000",
            "MODE": "prepare",
            "ALLOW_SHORTFALLS": "1",
        },
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert "ALLOW_SHORTFALLS" in proc.stdout + proc.stderr
    assert (tmp_path / "data" / "manual.csv").is_file()


def test_scripts_are_executable() -> None:
    for name in ("run.sh",):
        mode = (REPO / "examples" / name).stat().st_mode
        assert mode & stat.S_IXUSR, name
