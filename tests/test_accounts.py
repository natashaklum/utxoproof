"""Plan v8 account manifests, descriptor derivation and per-output ownership."""

from __future__ import annotations

from pathlib import Path

import pytest
from embit import bip32

from utxoproof.accounts import (
    AccountError,
    account_for_script,
    attribute_outputs,
    load_account,
    load_registry,
    persist_registry,
    portfolio_config,
)
from utxoproof.db import open_memory_db
from utxoproof.descriptors import (
    derive_descriptor_scripts,
    normalize_descriptor_pair,
)

BIP84_ZPUB = (
    "zpub6rFR7y4Q2AijBEqTUquhVz398htDFrtymD9xYYfG1m4wAcvPhXNfE3EfH1r1ADqtfSdVCTo"
    "UG868RvUUkgDKf31mGDtKsAYz2oz2AGutZYs"
)
BISQ_LEGACY_XPUB = (
    "xpub6CDEarkRoiwWPj3n3gYygGwgoGchxYg3g6Zs5L2nB4B6wdojzcWCKKHMu9XuY1GyYygRfrVembjAko1T5x"
    "Tsxj7ecKXxEPzDxx7nCK8Dxtx"
)
BISQ_SEGWIT_ZPUB = (
    "zpub6qsmCC6G762U9EBWshJ26LaGg2LS68YP9gfsbJ1jcwEP2MShvyXgw19BLkYn9D5Qs45UmJUfAKMSHkVp"
    "fiDVcMcB7q2oALJ3nJUnbjmfUE8"
)
FINGERPRINT = "73c5da0a"


def _input_tree(tmp_path: Path) -> Path:
    root = tmp_path / "input"
    (root / "accounts").mkdir(parents=True)
    return root


def _wallet(root: Path, account_id: str = "ledger-main", **extra: object) -> Path:
    folder = root / "accounts" / account_id
    folder.mkdir(parents=True)
    values = {
        "kind": "wallet",
        "label": "Ledger main",
        "xpub": BIP84_ZPUB,
        "fingerprint": FINGERPRINT,
        "gap_limit": 3,
        **extra,
    }
    lines = [f'{key} = "{value}"' for key, value in values.items()]
    (folder / "account.toml").write_text("\n".join(lines) + "\n")
    return folder


def test_load_wallet_account_and_derive_scripts(tmp_path: Path) -> None:
    root = _input_tree(tmp_path)
    _wallet(root)
    registry = load_registry(root)
    assert registry.ok, registry.errors
    account = registry.get("ledger-main")
    assert account.kind == "wallet"
    assert account.wallet == "ledger-main"
    assert len(account.scripts()) == 6
    assert account.scripts()[0][0:2] == (
        "0",
        "bc1qcr8te4kr609gcawutmrza0j4xv80jy8z306fyu",
    )
    accounts, fiat, _tags = portfolio_config(registry)
    assert accounts[0].wallet == "ledger-main"
    assert fiat == []


def test_wallet_xpub_shorthand_accepts_bisq_keychains(tmp_path: Path) -> None:
    root = _input_tree(tmp_path)
    legacy = root / "accounts" / "bisq-legacy"
    legacy.mkdir(parents=True)
    (legacy / "account.toml").write_text(
        'kind = "wallet"\n'
        'label = "Bisq legacy"\n'
        "gap_limit = 2\n"
        f'xpub = "{BISQ_LEGACY_XPUB}"\n'
        "purpose = 44\n"
        'script_type = "pkh"\n',
        encoding="utf-8",
    )
    segwit = root / "accounts" / "bisq-segwit"
    segwit.mkdir(parents=True)
    (segwit / "account.toml").write_text(
        'kind = "wallet"\n'
        'label = "Bisq segwit"\n'
        "gap_limit = 2\n"
        f'xpub = "{BISQ_SEGWIT_ZPUB}"\n'
        'script_type = "wpkh"\n'
        "derivation = \"m/44'/0'/1'\"\n",
        encoding="utf-8",
    )
    registry = load_registry(root)
    assert registry.ok, registry.errors
    legacy_account = registry.get("bisq-legacy")
    assert legacy_account.descriptors["external"].startswith("pkh(")
    assert legacy_account.scripts()[0][1][0] in ("1", "m", "n")
    segwit_account = registry.get("bisq-segwit")
    assert segwit_account.descriptors["external"].startswith("wpkh(")
    assert segwit_account.scripts()[0][1].startswith("bc1")


def test_exchange_wallet_accounts_must_be_wallets(tmp_path: Path) -> None:
    root = _input_tree(tmp_path)
    _wallet(root)
    for exchange_id, target in (("manual-a", "ledger-main"), ("manual-b", "missing")):
        folder = root / "accounts" / exchange_id
        folder.mkdir(parents=True)
        (folder / "events.csv").write_text("date,kind,btc\n", encoding="utf-8")
        (folder / "account.toml").write_text(
            'kind = "exchange"\n'
            f'label = "{exchange_id}"\n'
            'exchange = "manual"\n'
            'events = "events.csv"\n'
            f'wallet_accounts = ["{target}"]\n',
            encoding="utf-8",
        )
    registry = load_registry(root)
    assert not registry.ok
    assert any("unknown account 'missing'" in error for error in registry.errors)


def test_wallet_cannot_be_linked_to_two_exchanges(tmp_path: Path) -> None:
    root = _input_tree(tmp_path)
    _wallet(root)
    for exchange_id in ("manual-a", "manual-b"):
        folder = root / "accounts" / exchange_id
        folder.mkdir(parents=True)
        (folder / "events.csv").write_text("date,kind,btc\n", encoding="utf-8")
        (folder / "account.toml").write_text(
            'kind = "exchange"\n'
            f'label = "{exchange_id}"\n'
            'exchange = "manual"\n'
            'events = "events.csv"\n'
            'wallet_accounts = ["ledger-main"]\n',
            encoding="utf-8",
        )
    registry = load_registry(root)
    assert not registry.ok
    assert any("linked to both exchange accounts" in error for error in registry.errors)


def test_wallet_accounts_reject_non_wallet(tmp_path: Path) -> None:
    root = _input_tree(tmp_path)
    bank = root / "accounts" / "bank"
    bank.mkdir(parents=True)
    (bank / "statements").mkdir()
    (bank / "statements" / "jan.csv").write_text("date,amount\n", encoding="utf-8")
    (bank / "account.toml").write_text(
        'kind = "bank"\nlabel = "Bank"\nprofile = "kbc-bankaccount"\n',
        encoding="utf-8",
    )
    folder = root / "accounts" / "manual"
    folder.mkdir(parents=True)
    (folder / "events.csv").write_text("date,kind,btc\n", encoding="utf-8")
    (folder / "account.toml").write_text(
        'kind = "exchange"\n'
        'label = "Manual"\n'
        'exchange = "manual"\n'
        'events = "events.csv"\n'
        'wallet_accounts = ["bank"]\n',
        encoding="utf-8",
    )
    registry = load_registry(root)
    assert not registry.ok
    assert any("is bank, not a wallet" in error for error in registry.errors)


def test_multipath_descriptor_file_is_split(tmp_path: Path) -> None:
    root = _input_tree(tmp_path)
    folder = root / "accounts" / "descriptor-wallet"
    folder.mkdir(parents=True)
    descriptor = f"wpkh([{FINGERPRINT}/84h/0h/0h]{BIP84_ZPUB}/<0;1>/*)"
    (folder / "receive.txt").write_text(descriptor + "\n")
    (folder / "account.toml").write_text(
        'kind = "wallet"\nlabel = "Descriptor wallet"\ngap_limit = 2\n'
    )
    registry = load_registry(root)
    assert registry.ok, registry.errors
    account = registry.get("descriptor-wallet")
    assert "/0/*" in account.descriptors["external"]
    assert "/1/*" in account.descriptors["change"]
    assert len(account.scripts()) == 4


def test_single_descriptor_without_change_is_rejected(tmp_path: Path) -> None:
    root = _input_tree(tmp_path)
    folder = root / "accounts" / "bad"
    folder.mkdir(parents=True)
    (folder / "receive.txt").write_text(f"wpkh([{FINGERPRINT}/84h/0h/0h]{BIP84_ZPUB}/0/*)\n")
    (folder / "account.toml").write_text('kind = "wallet"\n')
    registry = load_registry(root)
    assert not registry.ok
    assert any("change descriptor" in error for error in registry.errors)


def test_duplicate_wallet_scripts_are_a_hard_error(tmp_path: Path) -> None:
    root = _input_tree(tmp_path)
    _wallet(root, "one")
    _wallet(root, "two")
    registry = load_registry(root)
    assert not registry.ok
    assert any("derive the same script" in error for error in registry.errors)


def test_bank_credit_card_exchange_and_broker_manifests(tmp_path: Path) -> None:
    root = _input_tree(tmp_path)
    bank = root / "accounts" / "kbc"
    (bank / "statements" / "2023").mkdir(parents=True)
    (bank / "statements" / "2023" / "jan.csv").write_text("date,amount\n")
    (bank / "account.toml").write_text(
        'kind = "bank"\nlabel = "KBC"\nprofile = "kbc-bankaccount"\n'
        '[[balances]]\namount_eur = "1234.56"\nas_of = "2023-12-31"\n'
    )
    card = root / "accounts" / "visa"
    (card / "statements").mkdir(parents=True)
    (card / "statements" / "card.csv").write_text("date,amount\n")
    (card / "account.toml").write_text(
        'kind = "credit_card"\nlabel = "Visa"\nprofile = "kbc-creditcard"\n'
        '[[balances]]\namount_eur = "-250.00"\n'
    )
    exchange = root / "accounts" / "kraken"
    exchange.mkdir()
    (exchange / "ledgers.csv").write_text("txid,refid,time,type,asset,amount\n")
    (exchange / "account.toml").write_text(
        'kind = "exchange"\nlabel = "Kraken"\nexchange = "kraken"\nledgers = "ledgers.csv"\n'
    )
    broker = root / "accounts" / "broker"
    broker.mkdir()
    (broker / "account.toml").write_text(
        'kind = "broker"\nlabel = "Broker"\nbroker = "degiro"\n'
        '[[holdings]]\nsymbol = "IWDA"\nquantity = "10"\nvalue_eur = "850.00"\n'
    )
    registry = load_registry(root)
    assert registry.ok, registry.errors
    assert len(registry.get("kbc").statements) == 1
    assert registry.get("visa").kind == "credit_card"
    assert len(registry.get("kraken").ledgers) == 1
    assert registry.get("broker").holdings[0].symbol == "IWDA"
    _accounts, fiat, _tags = portfolio_config(registry)
    assert {f.entity_id for f in fiat} == {"kbc", "visa"}


def test_manifest_ignores_legacy_junk_lines(tmp_path: Path) -> None:
    root = _input_tree(tmp_path)
    folder = root / "accounts" / "kbc"
    (folder / "statements").mkdir(parents=True)
    (folder / "statements" / "jan.csv").write_text("date,amount\n")
    (folder / "account.toml").write_text(
        'kind = "bank"\n'
        'label = "KBC"\n'
        'profile = "kbc-bankaccount"\n'
        "# old comment from a previous setup\n"
        "notes: migrated from v7\n"
        "tags = [fiat, legacy]\n"
        'extra = ["valid", "list"]\n',
        encoding="utf-8",
    )
    registry = load_registry(root)
    assert registry.ok, registry.errors
    assert registry.get("kbc").tags == ()
    assert any("there were invalid lines in" in warning for warning in registry.warnings)
    log = folder / "WARNINGS_account.toml.log"
    assert log.is_file()
    details = log.read_text(encoding="utf-8")
    assert "ignored invalid TOML line 5" in details
    assert "ignored invalid TOML line 6" in details


def test_manifest_with_broken_required_line_still_fails(tmp_path: Path) -> None:
    root = _input_tree(tmp_path)
    folder = root / "accounts" / "broken"
    folder.mkdir(parents=True)
    (folder / "account.toml").write_text('label = "Broken"\nkind: wallet\n', encoding="utf-8")
    registry = load_registry(root)
    assert not registry.ok
    assert any("kind must be one of" in error for error in registry.errors)


def test_persist_registry_and_attribute_outputs(tmp_path: Path) -> None:
    root = _input_tree(tmp_path)
    _wallet(root)
    registry = load_registry(root)
    db = open_memory_db()
    persist_registry(db, registry)
    address, script_hex = registry.get("ledger-main").scripts()[0][1:]
    db.execute(
        "INSERT INTO transactions (txid, block_time) VALUES ('a'*64, '2024-01-01T00:00:00+00:00')"
    )
    db.execute(
        "INSERT INTO tx_outputs (txid, vout, address, script_hex, value_sat, is_mine) "
        "VALUES (?,0,?,?,?,1)",
        ("a" * 64, address, script_hex, 100_000_000),
    )
    db.execute(
        "INSERT INTO tx_outputs (txid, vout, address, script_hex, value_sat, is_mine) "
        "VALUES (?,1,?,?,?,1)",
        ("a" * 64, "bc1qexternal", "0014" + "11" * 20, 50_000_000),
    )
    assert attribute_outputs(db, registry, strict=True) == 1
    row = db.execute("SELECT account_id, is_mine FROM tx_outputs WHERE vout=0").fetchone()
    assert row == ("ledger-main", 1)
    row = db.execute("SELECT account_id, is_mine FROM tx_outputs WHERE vout=1").fetchone()
    assert row == (None, 0)
    assert account_for_script(db, script_hex) == "ledger-main"


def test_descriptor_helpers_cover_multisig(tmp_path: Path) -> None:
    # BIP32 test-vector master public key plus BIP84 account key as two cosigners.
    second = bip32.HDKey.from_seed(bytes.fromhex("000102030405060708090a0b0c0d0e0f"))
    second_xpub = second.derive("m/48h/0h/0h").to_public().to_base58()
    descriptor = (
        "wsh(sortedmulti(1,"
        f"[{FINGERPRINT}/48h/0h/0h]{BIP84_ZPUB}/0/*,"
        f"[3442193e/48h/0h/0h]{second_xpub}/0/*))"
    )
    external, change = normalize_descriptor_pair(
        descriptor.replace("/0/*", "/<0;1>/*"), network="mainnet"
    )
    assert external.endswith("#" + external.rsplit("#", 1)[1])
    scripts = derive_descriptor_scripts(external, branch=0, start=0, count=2)
    assert len(scripts) == 2
    assert scripts[0][1].startswith("0020")  # P2WSH
    assert scripts[0][1] != scripts[1][1]
    assert change


def test_bad_checksum_is_rejected(tmp_path: Path) -> None:
    from utxoproof.descriptors import parse_descriptor

    with pytest.raises(ValueError, match="checksum"):
        parse_descriptor(f"wpkh({BIP84_ZPUB}/0/*)#00000000")


def test_private_extended_keys_are_rejected(tmp_path: Path) -> None:
    root = _input_tree(tmp_path)
    folder = root / "accounts" / "leaky"
    folder.mkdir(parents=True)
    (folder / "account.toml").write_text(
        'kind = "wallet"\n'
        'label = "Leaky"\n'
        'fingerprint = "73c5da0a"\n'
        'xpub = "xprv9s21ZrQH143K3abcdefghijklmnopqrstuvwxyz"\n',
        encoding="utf-8",
    )
    with pytest.raises(AccountError, match="private extended keys"):
        load_account(folder)
