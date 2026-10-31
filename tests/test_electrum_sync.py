"""Electrum-backed account sync against an in-process fake server protocol."""

from __future__ import annotations

from typing import Any, cast

from embit.script import Script
from embit.transaction import Transaction, TransactionInput, TransactionOutput

from utxoproof.accounts import Account
from utxoproof.db import open_memory_db
from utxoproof.descriptors import (
    build_descriptors,
    derive_descriptor_scripts,
    normalize_descriptor_pair,
)
from utxoproof.electrum import ElectrumClient, scripthash
from utxoproof.electrum_sync import ElectrumOnchainImporter

BIP84_ZPUB = (
    "zpub6rFR7y4Q2AijBEqTUquhVz398htDFrtymD9xYYfG1m4wAcvPhXNfE3EfH1r1ADqtfSdVCTo"
    "UG868RvUUkgDKf31mGDtKsAYz2oz2AGutZYs"
)
FINGERPRINT = "73c5da0a"


def _account() -> tuple[Account, str, str]:
    built = build_descriptors(BIP84_ZPUB, FINGERPRINT)
    external, change = normalize_descriptor_pair(built["external"], built["change"])
    address, script_hex = derive_descriptor_scripts(external, count=1)[0]
    return (
        Account(
            id="wallet-a",
            kind="wallet",
            label="Wallet A",
            gap_limit=2,
            descriptors={"external": external, "change": change},
        ),
        address,
        script_hex,
    )


def _raw_tx(script_hex: str) -> str:
    tx = Transaction(
        version=2,
        vin=[TransactionInput(bytes.fromhex("11" * 32), 0)],
        vout=[TransactionOutput(150_000_000, Script(bytes.fromhex(script_hex)))],
        locktime=0,
    )
    return tx.serialize().hex()


class FakeServer:
    def __init__(self, script_hex: str, raw: str) -> None:
        self.target = scripthash(script_hex)
        tx = Transaction.parse(bytes.fromhex(raw))
        self.txid = tx.txid().hex()
        self.raw = raw
        header = bytearray(80)
        header[68:72] = (1_700_000_000).to_bytes(4, "little")
        self.header = header.hex()

    def _one(self, payload: dict[str, Any]) -> dict[str, Any]:
        method = payload["method"]
        params = payload["params"]
        if method == "blockchain.scripthash.get_history":
            history = [{"txid": self.txid, "height": 800_000}] if params[0] == self.target else []
            return {"id": payload["id"], "result": history, "error": None}
        if method == "blockchain.transaction.get":
            assert params[0] == self.txid
            return {"id": payload["id"], "result": self.raw, "error": None}
        if method == "blockchain.block.header":
            assert params[0] == 800_000
            return {"id": payload["id"], "result": self.header, "error": None}
        raise AssertionError(f"unexpected method {method}")

    def request(self, payload: object) -> object:
        if isinstance(payload, list):
            return [self._one(cast(dict[str, Any], item)) for item in payload]
        return self._one(cast(dict[str, Any], payload))

    def close(self) -> None:
        pass


def test_electrum_sync_attributes_outputs_and_extends_gap() -> None:
    account, address, script_hex = _account()
    raw = _raw_tx(script_hex)
    fake = FakeServer(script_hex, raw)
    client = ElectrumClient("ssl://electrum.test:50002", transport=fake)
    db = open_memory_db()
    importer = ElectrumOnchainImporter(client, db)
    summary = importer.sync_accounts([account])
    assert summary.txs_seen == 1
    assert summary.new_txs == 1
    assert summary.outputs_attributed == 1
    assert account.gap_limit == 4  # activity at index 0 extends by two
    row = db.execute("SELECT address, value_sat, is_mine, account_id FROM tx_outputs").fetchone()
    assert row == (address, 150_000_000, 1, "wallet-a")
    assert db.execute("SELECT block_height FROM transactions").fetchone() == (800_000,)
    assert db.execute("SELECT value FROM sync_state WHERE key='last_backend'").fetchone() == (
        "electrum",
    )


def test_electrum_sync_is_idempotent() -> None:
    account, _address, script_hex = _account()
    raw = _raw_tx(script_hex)
    fake = FakeServer(script_hex, raw)
    client = ElectrumClient("ssl://electrum.test:50002", transport=fake)
    db = open_memory_db()
    importer = ElectrumOnchainImporter(client, db)
    assert importer.sync_accounts([account]).new_txs == 1
    account.gap_limit = 2  # fresh edge; same server data
    second = ElectrumOnchainImporter(client, db).sync_accounts([account])
    assert second.new_txs == 0
    assert db.execute("SELECT COUNT(*) FROM transactions").fetchone() == (1,)
    assert db.execute("SELECT COUNT(*) FROM tx_outputs").fetchone() == (1,)


def test_reconcile_electrum_uses_derived_scripts_and_per_account_balance(
    tmp_path, monkeypatch, capsys
) -> None:
    import argparse

    from utxoproof import electrum
    from utxoproof.accounts import load_registry
    from utxoproof.cli import _reconcile_electrum

    account_dir = tmp_path / "input" / "accounts" / "wallet-a"
    account_dir.mkdir(parents=True)
    (account_dir / "account.toml").write_text(
        "\n".join(
            [
                'kind = "wallet"',
                'label = "Wallet A"',
                "gap_limit = 2",
                f'xpub = "{BIP84_ZPUB}"',
                f'fingerprint = "{FINGERPRINT}"',
            ]
        ),
        encoding="utf-8",
    )
    registry = load_registry(tmp_path / "input")
    assert registry.ok, registry.errors
    account = registry.by_kind("wallet")[0]
    _chain, address, script_hex = account.scripts()[0]
    target = scripthash(script_hex)
    db = open_memory_db()
    db.execute(
        "INSERT INTO tx_outputs (txid,vout,address,value_sat,is_mine,account_id,script_hex) "
        "VALUES (?,?,?,?,?,?,?)",
        ("aa" * 32, 0, address, 150_000_000, 1, account.id, script_hex),
    )

    class FakeClient:
        def __init__(self, _url: str, **_kwargs: object) -> None:
            pass

        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *_args: object) -> None:
            pass

        def batch(self, calls: list[tuple[str, list[object]]]) -> list[dict[str, int]]:
            return [
                {"confirmed": 150_000_000, "unconfirmed": 0}
                if params[0] == target
                else {"confirmed": 0, "unconfirmed": 0}
                for _method, params in calls
            ]

    monkeypatch.setattr(electrum, "ElectrumClient", FakeClient)
    code = _reconcile_electrum(
        argparse.Namespace(input=str(tmp_path / "input"), electrum_url=None, ca_file=None),
        db,
    )
    output = capsys.readouterr().out
    db.close()
    assert code == 0
    assert "wallet-a: db=1.5 BTC electrum=1.5 BTC [OK]" in output
    assert "RECONCILED (gap 0)" in output


def test_change_only_activity_extends_the_persisted_gap() -> None:
    built = build_descriptors(BIP84_ZPUB, FINGERPRINT)
    external, change = normalize_descriptor_pair(built["external"], built["change"])
    _address, script_hex = derive_descriptor_scripts(change, count=1)[0]
    account = Account(
        id="wallet-a",
        kind="wallet",
        label="Wallet A",
        gap_limit=2,
        descriptors={"external": external, "change": change},
    )
    raw = _raw_tx(script_hex)
    fake = FakeServer(script_hex, raw)
    client = ElectrumClient("ssl://electrum.test:50002", transport=fake)
    db = open_memory_db()
    summary = ElectrumOnchainImporter(client, db).sync_accounts([account])
    assert summary.new_txs == 1
    assert account.gap_limit == 4
    assert db.execute("SELECT is_mine, account_id FROM tx_outputs").fetchone() == (1, "wallet-a")
