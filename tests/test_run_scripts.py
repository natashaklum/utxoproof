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
    merged.setdefault(
        "UTXOPROOF_BIN", os.path.join(os.path.dirname(__import__("sys").executable), "utxoproof")
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


def test_refresh_covers_every_data_year_by_default(tmp_path: Path) -> None:
    from utxoproof.cli import csv_years

    data = tmp_path / "data"
    proc = _run(
        "run.sh",
        {
            "DATA_DIR": str(data),
            "KRAKEN_LEDGERS": str(FIXTURES / "kraken_ledgers_2023.csv"),
            "KRAKEN_TRADES": "",
            "TAX_YEAR": "",
            "BTC_PRICE": "40000",
            "MODE": "refresh",
        },
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    years = csv_years(data / "manual.csv")
    assert len(years) >= 1
    for year in years:
        assert (data / str(year) / "report.html").is_file()


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


def test_refresh_converts_bank_statement(tmp_path: Path) -> None:
    data = tmp_path / "data"
    proc = _run(
        "run.sh",
        {
            "DATA_DIR": str(data),
            "KRAKEN_LEDGERS": str(FIXTURES / "kraken_ledgers_2023.csv"),
            "KRAKEN_TRADES": "",
            "BANK_CSV": str(FIXTURES / "kbc_2023.csv"),
            "BANK_TYPE": "kbc",
            "TAX_YEAR": "2023",
            "BTC_PRICE": "40000",
            "MODE": "refresh",
        },
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    bank_rows = data / "bank-rows.csv"
    assert bank_rows.is_file()
    assert "BINANCE storting" in bank_rows.read_text(encoding="utf-8")


def test_refresh_bank_page_links_nav_and_matches(tmp_path: Path) -> None:
    data = tmp_path / "data"
    proc = _run(
        "run.sh",
        {
            "DATA_DIR": str(data),
            "KRAKEN_LEDGERS": str(FIXTURES / "kraken_ledgers_2023.csv"),
            "KRAKEN_TRADES": "",
            "BANK_CSV": str(FIXTURES / "kbc_2023.csv"),
            "BANK_TYPE": "kbc",
            "TAX_YEAR": "2023",
            "BTC_PRICE": "40000",
            "MODE": "refresh",
        },
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    bank_html = data / "bank" / "bank.html"
    assert bank_html.is_file()
    html = bank_html.read_text(encoding="utf-8")
    assert '<nav class="site-nav">' in html  # real menu element, not just CSS
    # Planned menu: the overview link is present even though no overview was
    # built in this run (no entities/node) — the skip note says how to get it.
    assert "../portfolio/overview/overview.html" in html
    assert "<strong>Bank review</strong>" in html  # current page marked
    report_html = data / "2023" / "report.html"
    report_text = report_html.read_text(encoding="utf-8")
    assert '<nav class="site-nav">' in report_text
    assert "../bank/bank.html" in report_text  # tax page reaches bank review
    assert "<strong>Tax 2023</strong>" in report_text
    alltime_text = (data / "alltime" / "summary.html").read_text(encoding="utf-8")
    assert '<nav class="site-nav">' in alltime_text
    assert "../2023/report.html" in alltime_text


def test_refresh_merges_several_bank_statements(tmp_path: Path) -> None:
    data = tmp_path / "data"
    kbc = FIXTURES / "kbc_2023.csv"
    ing = FIXTURES / "ing_2023.csv"
    proc = _run(
        "run.sh",
        {
            "DATA_DIR": str(data),
            "KRAKEN_LEDGERS": str(FIXTURES / "kraken_ledgers_2023.csv"),
            "KRAKEN_TRADES": "",
            "BANK_CSV": f"kbc:{kbc}|ing:{ing}",
            "BANK_TYPE": "kbc",
            "TAX_YEAR": "2023",
            "BTC_PRICE": "40000",
            "MODE": "refresh",
        },
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    merged = (data / "bank-rows.csv").read_text(encoding="utf-8")
    assert "BINANCE storting" in merged
    assert "KRAKEN overschrijving" in merged
    html = (data / "bank" / "bank.html").read_text(encoding="utf-8")
    assert "kbc_2023.csv" in html and "ing_2023.csv" in html
    zips = list((data / "2023").glob("utxoproof_evidence_2023_*.zip"))
    assert len(zips) == 1
    import zipfile

    names = zipfile.ZipFile(zips[0]).namelist()
    assert any("kbc_2023.csv" in n for n in names)
    assert any("ing_2023.csv" in n for n in names)


def test_refresh_bank_paths_with_spaces(tmp_path: Path) -> None:
    import shutil

    data = tmp_path / "data"
    spaced = tmp_path / "my exports"
    spaced.mkdir()
    kbc = spaced / "kbc statement.csv"
    shutil.copy(FIXTURES / "kbc_2023.csv", kbc)
    proc = _run(
        "run.sh",
        {
            "DATA_DIR": str(data),
            "KRAKEN_LEDGERS": str(FIXTURES / "kraken_ledgers_2023.csv"),
            "KRAKEN_TRADES": "",
            "BANK_CSV": f"kbc:{kbc}",
            "BANK_TYPE": "kbc",
            "TAX_YEAR": "2023",
            "BTC_PRICE": "40000",
            "MODE": "refresh",
        },
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert (data / "bank" / "bank.html").is_file()
    html = (data / "bank" / "bank.html").read_text(encoding="utf-8")
    assert "kbc statement.csv" in html
    assert "BINANCE storting" in html


def test_refresh_mixed_encoding_bank_statements(tmp_path: Path) -> None:
    import shutil

    data = tmp_path / "data"
    utf8 = tmp_path / "utf8.csv"
    shutil.copy(FIXTURES / "kbc_2023.csv", utf8)
    latin = tmp_path / "latin.csv"
    latin.write_bytes(
        "Datum;Omschrijving;Tegenpartijrekening;Bedrag;Mededeling\n"
        "11/01/2023;KBC Belgiës;BE68;100,00;R9\n".encode("cp1252")
    )
    proc = _run(
        "run.sh",
        {
            "DATA_DIR": str(data),
            "KRAKEN_LEDGERS": str(FIXTURES / "kraken_ledgers_2023.csv"),
            "KRAKEN_TRADES": "",
            "BANK_CSV": f"kbc:{utf8}|kbc:{latin}",
            "BANK_TYPE": "kbc",
            "TAX_YEAR": "2023",
            "BTC_PRICE": "40000",
            "MODE": "refresh",
        },
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    html = (data / "bank" / "bank.html").read_text(encoding="utf-8")
    assert "BINANCE storting" in html and "KBC Belgiës" in html


def test_run_loops_setup_and_sync_per_wallet(tmp_path: Path) -> None:
    log = tmp_path / "calls.log"
    stub = tmp_path / "utxoproof"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        'echo "STUB $@" >> "$STUB_LOG"\n'
        'if [ "$1" = "check" ]; then printf "ok\\nshortfalls: 0\\n"; fi\n'
        "exit 0\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    env = {
        "DATA_DIR": str(tmp_path / "data"),
        "KRAKEN_LEDGERS": str(FIXTURES / "kraken_ledgers_2023.csv"),
        "KRAKEN_TRADES": "",
        "TAX_YEAR": "2023",
        "MODE": "prepare",
        "ALLOW_SHORTFALLS": "1",
        "UTXOPROOF_BIN": str(stub),
        "STUB_LOG": str(log),
        "WALLETS": "cold:AA:xpub-cold|hot:BB:xpub-hot",
    }
    proc = _run("run.sh", env)
    assert proc.returncode == 0, proc.stderr[-2000:]
    calls = log.read_text(encoding="utf-8")
    assert "--wallet cold" in calls and "--xpub xpub-cold" in calls
    assert "--wallet hot" in calls and "--xpub xpub-hot" in calls
    assert "--fingerprint AA" in calls and "--fingerprint BB" in calls
    assert calls.count("sync --db") == 2


def test_run_rejects_malformed_wallet_entry(tmp_path: Path) -> None:
    proc = _run(
        "run.sh",
        {
            "DATA_DIR": str(tmp_path / "data"),
            "KRAKEN_LEDGERS": str(FIXTURES / "kraken_ledgers_2023.csv"),
            "KRAKEN_TRADES": "",
            "TAX_YEAR": "2023",
            "MODE": "prepare",
            "ALLOW_SHORTFALLS": "1",
            "WALLETS": "bogus-without-colons",
        },
    )
    assert proc.returncode != 0
    assert "bad WALLETS entry" in proc.stdout + proc.stderr


def test_clean_is_a_dry_run_by_default(tmp_path: Path) -> None:
    data = tmp_path / "data"
    (data / "portfolio" / "overview").mkdir(parents=True)
    (data / "portfolio" / "overview" / "overview.html").write_text("page", encoding="utf-8")
    (data / "2023").mkdir()
    (data / "2023" / "report.html").write_text("page", encoding="utf-8")
    (data / "manual.csv").write_text("date,kind\n", encoding="utf-8")
    source = tmp_path / "ledgers.csv"
    source.write_text("source", encoding="utf-8")

    proc = _run("run.sh", {"DATA_DIR": str(data), "MODE": "clean"})
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert "would remove:" in proc.stdout
    assert "CONFIRM=1" in proc.stdout
    assert (data / "manual.csv").is_file()
    assert (data / "portfolio" / "overview" / "overview.html").is_file()
    assert (data / "2023" / "report.html").is_file()
    assert source.is_file()


def test_clean_confirm_removes_generated_artifacts_but_not_sources(tmp_path: Path) -> None:
    data = tmp_path / "data"
    (data / "portfolio").mkdir(parents=True)
    (data / "portfolio" / "overview.html").write_text("page", encoding="utf-8")
    (data / "2023").mkdir()
    (data / "2023" / "report.html").write_text("page", encoding="utf-8")
    (data / "manual.csv").write_text("date,kind\n", encoding="utf-8")
    (data / "bank-rows.csv").write_text("date,kind\n", encoding="utf-8")
    (data / "bank-rows-1.csv").write_text("date,kind\n", encoding="utf-8")
    (data / "utxoproof.db").write_bytes(b"db")
    source = tmp_path / "ledgers.csv"
    source.write_text("source", encoding="utf-8")

    proc = _run("run.sh", {"DATA_DIR": str(data), "MODE": "clean", "CONFIRM": "1"})
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert "removed:" in proc.stdout
    assert not (data / "manual.csv").exists()
    assert not (data / "bank-rows.csv").exists()
    assert not (data / "bank-rows-1.csv").exists()
    assert not (data / "portfolio").exists()
    assert not (data / "2023").exists()
    assert not (data / "utxoproof.db").exists()
    assert data.is_dir()
    assert source.is_file()
