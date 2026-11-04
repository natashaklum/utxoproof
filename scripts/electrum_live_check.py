#!/usr/bin/env python3
"""Live Electrum smoke check against a public server and public test vector.

The default account is the BIP84 spec vector's account 1
(``m/84'/0'/1'``), a deliberately public watch-only key with a handful of
mainnet transactions. No private key or real user data is involved. The check
runs the full v8 ``run`` pipeline, then compares DB-unspent sats with the
server's scripthash balances so a broken spend link cannot pass silently.

``ssl://electrum.blockstream.info:50002`` is the default. Public servers see
the queried addresses; use your own Electrum server for real wallets.
"""

from __future__ import annotations

import argparse
import sqlite3
import tempfile
from pathlib import Path
from typing import Any, cast

from utxoproof.accounts import load_registry
from utxoproof.electrum import ElectrumClient, scripthash
from utxoproof.runner import RunOptions, run

PUBLIC_URL = "ssl://electrum.blockstream.info:50002"
# BIP84 test vector account 1 (m/84'/0'/1'), account-level xpub.
PUBLIC_XPUB = (
    "xpub6CatWdiZiodmYVtWLtEQsAg1H9ooS1bmsJUBwQ83FE1Fyk386FWcyicJgEZv3quZSJKA5dh5Lo2"
    "PbubMGxCfZtRthV6ST2qquL9w3HSzcUn"
)
PUBLIC_FINGERPRINT = "73c5da0a"
ACCOUNT_ID = "public-test-vector"


def _remote_balance(client: ElectrumClient, script_hex: str) -> int:
    payload = client.call("blockchain.scripthash.get_balance", scripthash(script_hex))
    if not isinstance(payload, dict):
        raise RuntimeError("get_balance returned a non-object response")
    data = cast(dict[str, Any], payload)
    return int(data.get("confirmed", 0)) + int(data.get("unconfirmed", 0))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=PUBLIC_URL, help="Electrum server URL")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--gap-limit", type=int, default=5)
    parser.add_argument("--xpub", default=PUBLIC_XPUB)
    parser.add_argument("--fingerprint", default=PUBLIC_FINGERPRINT)
    parser.add_argument("--purpose", type=int, default=84)
    parser.add_argument("--account", type=int, default=1)
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="utxoproof-live-") as temp:
        root = Path(temp)
        input_dir = root / "input"
        account_dir = input_dir / "accounts" / ACCOUNT_ID
        account_dir.mkdir(parents=True)
        (input_dir / "utxoproof.toml").write_text(
            "\n".join(
                [
                    "[owner]",
                    'name = "Live Electrum smoke check"',
                    "",
                    "[chain]",
                    'backend = "electrum"',
                    'network = "mainnet"',
                    f'electrum_url = "{args.url}"',
                    "",
                    "[reporting]",
                    'tax_year = "2023"',
                    "",
                ]
            ),
            encoding="utf-8",
        )
        (account_dir / "account.toml").write_text(
            "\n".join(
                [
                    'kind = "wallet"',
                    'label = "BIP84 public test vector account 1"',
                    'network = "mainnet"',
                    f"gap_limit = {args.gap_limit}",
                    f'xpub = "{args.xpub}"',
                    f'fingerprint = "{args.fingerprint}"',
                    f"purpose = {args.purpose}",
                    f"account = {args.account}",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        registry = load_registry(input_dir)
        if not registry.ok:
            raise RuntimeError("; ".join(registry.errors))
        data_dir = root / "data"
        code = run(
            RunOptions(
                input_dir=input_dir,
                data_dir=data_dir,
                year=2023,
                as_of="2023-12-31",
                no_zip=True,
            )
        )
        if code != 0:
            raise RuntimeError(f"v8 run failed with exit code {code}")

        db = sqlite3.connect(data_dir / "utxoproof.db")
        try:
            scripts = [
                str(row[0])
                for row in db.execute(
                    "SELECT script_hex FROM account_scripts WHERE account_id=? "
                    "ORDER BY chain, derivation_index",
                    (ACCOUNT_ID,),
                )
            ]
            outputs = db.execute(
                "SELECT COUNT(*), COUNT(DISTINCT txid) FROM tx_outputs WHERE account_id=?",
                (ACCOUNT_ID,),
            ).fetchone()
            db_unspent = int(
                db.execute(
                    "SELECT COALESCE(SUM(value_sat),0) FROM tx_outputs "
                    "WHERE account_id=? AND is_mine=1 AND spent_by_txid IS NULL",
                    (ACCOUNT_ID,),
                ).fetchone()[0]
            )
        finally:
            db.close()

    with ElectrumClient(args.url, timeout=args.timeout) as client:
        version = client.call("server.version", "utxoproof-live-check", "1.4")
        height = client.call("blockchain.headers.subscribe")
        remote_unspent = sum(_remote_balance(client, script) for script in scripts)

    if not outputs or int(outputs[0]) == 0:
        raise RuntimeError("live sync found no outputs for the public test vector")
    if int(outputs[1]) == 0:
        raise RuntimeError("live sync found no transactions")
    if db_unspent != remote_unspent:
        raise RuntimeError(
            f"spend-link mismatch: db={db_unspent} sats, server={remote_unspent} sats"
        )

    print(f"server: {version}")
    print(f"height: {height}")
    print(f"account: {ACCOUNT_ID} ({args.xpub[:16]}...)")
    print(
        f"ok: {int(outputs[1])} tx(s), {int(outputs[0])} output(s), "
        f"{db_unspent} unspent sats match the server"
    )
    print("LIVE ELECTRUM OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
