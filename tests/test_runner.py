"""Plan v8 discovery and end-to-end offline runner."""

from __future__ import annotations

from pathlib import Path

import pytest

from utxoproof.accounts import load_registry
from utxoproof.runner import RunOptions, build_run_plan, discover, run

EXAMPLE = Path(__file__).parent.parent / "examples" / "input_v8"


def test_discover_example_tree() -> None:
    result = discover(EXAMPLE)
    assert result["ok"], result["errors"]
    assert result["owner"] == "Demo owner"
    assert {account["kind"] for account in result["accounts"]} == {
        "wallet",
        "bank",
        "credit_card",
        "exchange",
        "broker",
    }
    assert result["checks"]["labels"] == 4
    assert result["checks"]["bank_rows"] == 6
    assert result["checks"]["exchange_txs"] == 13
    assert result["checks"]["receipts"] == 2


def test_plan_lists_all_steps() -> None:
    registry = load_registry(EXAMPLE)
    plan = build_run_plan(registry, RunOptions(input_dir=EXAMPLE, data_dir="unused", plan=True))
    assert [item["step"] for item in plan] == [
        "labels",
        "sync",
        "exchanges",
        "banks",
        "report",
        "portfolio",
    ]
    assert all(item["selected"] for item in plan)


def test_plan_only_and_offline_selection() -> None:
    registry = load_registry(EXAMPLE)
    plan = build_run_plan(
        registry,
        RunOptions(
            input_dir=EXAMPLE,
            data_dir="unused",
            plan=True,
            only=("report", "portfolio"),
            offline=True,
        ),
    )
    selected = [item["step"] for item in plan if item["selected"]]
    assert selected == ["report", "portfolio"]


def test_offline_run_builds_account_site(tmp_path: Path) -> None:
    before = {
        path.relative_to(EXAMPLE): path.read_bytes()
        for path in EXAMPLE.rglob("*")
        if path.is_file()
    }
    data = tmp_path / "data"
    code = run(
        RunOptions(
            input_dir=EXAMPLE,
            data_dir=data,
            offline=True,
            year=2023,
            as_of="2023-12-31",
        )
    )
    assert code == 0
    assert (data / "utxoproof.db").is_file()
    assert (data / "imports" / "manual.csv").is_file()
    assert (data / "imports" / "bank-rows.csv").is_file()
    assert (data / "imports" / "exchange-links.csv").is_file()
    assert (data / "reports" / "2023" / "report.html").is_file()
    assert (data / "reports" / "overview" / "overview.html").is_file()
    assert (data / "reports" / "entities" / "kbc-checking.html").is_file()
    assert (data / "reports" / "exchange" / "index.html").is_file()
    assert (data / "reports" / "exchange" / "bisq.html").is_file()
    assert (data / "reports" / "exchange" / "old-exchange.html").is_file()
    assert list((data / "reports" / "exchange" / "receipts" / "bisq").glob("*.pdf"))
    bisq_html = (data / "reports" / "exchange" / "bisq.html").read_text(encoding="utf-8")
    assert "BISQ-2023-0001" in bisq_html
    assert "20230227_ES7200495" in bisq_html
    assert "tier-exact" in bisq_html
    assert "Linked on-chain wallets" in bisq_html
    assert "Bisq BTC legacy" in bisq_html
    assert "Bisq BTC segwit" in bisq_html
    assert "bisq-wallet-segwit.html" in bisq_html
    bank_html = (data / "reports" / "bank" / "bank.html").read_text(encoding="utf-8")
    assert "alert-warn" in bank_html
    assert "row-unmatched" in bank_html
    import zipfile

    zips = list((data / "reports" / "2023").glob("utxoproof_evidence_2023_*.zip"))
    assert len(zips) == 1
    with zipfile.ZipFile(zips[0]) as archive:
        names = set(archive.namelist())
    assert "sources/accounts/ledger-main/account.toml" in names
    assert "sources/utxoproof.toml" not in names
    assert "intermediate/manual.csv" in names
    assert "intermediate/exchange-links.csv" in names
    assert "intermediate/exchange-trades.csv" in names
    assert "intermediate/exchange-transfers.csv" in names
    assert "intermediate/exchange-receipts.csv" in names
    assert any(name.startswith("sources/accounts/bisq/receipts/") for name in names)
    assert any(name.startswith("sources/accounts/old-exchange/evidence/") for name in names)
    import sqlite3

    db = sqlite3.connect(data / "utxoproof.db")
    accounts = db.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
    labels = db.execute("SELECT COUNT(*) FROM labels").fetchone()[0]
    scripts = db.execute("SELECT COUNT(*) FROM account_scripts").fetchone()[0]
    db.close()
    assert accounts == 11
    assert labels == 4
    assert scripts == 2120  # 3 wallets x 2 chains x 20 + 2 Bisq wallets x 2 chains x 500
    after = {
        path.relative_to(EXAMPLE): path.read_bytes()
        for path in EXAMPLE.rglob("*")
        if path.is_file()
    }
    assert after == before


def test_report_only_reuses_persisted_bank_rows(tmp_path: Path) -> None:
    data = tmp_path / "data"
    base = {
        "input_dir": EXAMPLE,
        "data_dir": data,
        "offline": True,
        "year": 2023,
        "as_of": "2023-12-31",
        "no_zip": True,
    }
    assert run(RunOptions(**base)) == 0
    assert run(RunOptions(**base, only=("report", "portfolio"))) == 0
    bisq_html = (data / "reports" / "exchange" / "bisq.html").read_text(encoding="utf-8")
    assert "tier-strong" in bisq_html
    assert "no bank row" not in bisq_html
    links = (data / "imports" / "exchange-links.csv").read_text(encoding="utf-8")
    assert "2023-02-27,-308.50" in links


def test_bad_only_step_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown --only"):
        run(
            RunOptions(
                input_dir=EXAMPLE,
                data_dir="unused",
                plan=True,
                only=("nope",),
            )
        )


def test_run_refuses_data_dir_inside_input(capsys) -> None:
    code = run(
        RunOptions(
            input_dir=EXAMPLE,
            data_dir=EXAMPLE / "generated",
            offline=True,
            year=2023,
        )
    )
    assert code == 2
    assert "outside the input tree" in capsys.readouterr().out
    assert not (EXAMPLE / "generated").exists()


def test_bank_only_plan_skips_chain_sync(tmp_path: Path) -> None:
    root = tmp_path / "input"
    account = root / "accounts" / "checking"
    account.mkdir(parents=True)
    (account / "account.toml").write_text(
        'kind = "bank"\nlabel = "Checking"\nprofile = "kbc-bankaccount"\n',
        encoding="utf-8",
    )
    (account / "statements").mkdir()
    (account / "statements" / "jan.csv").write_text("date,amount\n", encoding="utf-8")
    registry = load_registry(root)
    assert registry.ok, registry.errors
    plan = build_run_plan(registry, RunOptions(input_dir=root, data_dir=tmp_path / "data"))
    sync = next(item for item in plan if item["step"] == "sync")
    assert sync["selected"] is False
    assert sync["action"] == "no wallet accounts"


def test_offline_run_can_skip_the_evidence_zip(tmp_path: Path) -> None:
    data = tmp_path / "data"
    code = run(
        RunOptions(
            input_dir=EXAMPLE,
            data_dir=data,
            offline=True,
            year=2023,
            as_of="2023-12-31",
            no_zip=True,
        )
    )
    assert code == 0
    assert (data / "reports" / "2023" / "report.html").is_file()
    assert not list((data / "reports" / "2023").glob("*.zip"))
