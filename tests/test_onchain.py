"""onchain importer tests (mocked RPC, regtest-shaped payloads)."""

import httpx

from utxoproof.bitcoin_rpc import BitcoinRPC
from utxoproof.db import open_memory_db
from utxoproof.onchain import BitcoinCoreOnchainImporter

OUR_ADDR = "bcrt1qoursupposedchangeaddress000000000000000000"
EXT_ADDR = "bcrt1qexternalpaymentaddress0000000000000000000"

TX_COINBASE = {
    "txid": "aaa",
    "vin": [{"coinbase": "04ffff", "sequence": 0}],
    "vout": [
        {"value": 6.25, "n": 0, "scriptPubKey": {"address": OUR_ADDR}},
    ],
    "blockhash": "block1",
    "blocktime": 1700000000,
}

TX_SPEND = {
    "txid": "bbb",
    "vin": [{"txid": "aaa", "vout": 0, "sequence": 0}],
    "vout": [
        {"value": 1.0, "n": 0, "scriptPubKey": {"address": EXT_ADDR}},
        {"value": 5.2499, "n": 1, "scriptPubKey": {"address": OUR_ADDR}},
    ],
    "blockhash": "block2",
    "blocktime": 1700000600,
}


def _rpc() -> BitcoinRPC:
    raws = {"aaa": TX_COINBASE, "bbb": TX_SPEND}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        body = json.loads(request.content)
        method = body["method"]
        if method == "listtransactions":
            return httpx.Response(
                200, json={"result": [{"txid": "aaa"}, {"txid": "bbb"}], "error": None}
            )
        if method == "getrawtransaction":
            return httpx.Response(200, json={"result": raws[body["params"][0]], "error": None})
        if method == "getblockheader":
            heights = {"block1": 101, "block2": 102}
            return httpx.Response(
                200, json={"result": {"height": heights[body["params"][0]]}, "error": None}
            )
        if method == "getaddressinfo":
            return httpx.Response(
                200,
                json={"result": {"ismine": body["params"][0] == OUR_ADDR}, "error": None},
            )
        raise AssertionError(f"unexpected method {method}")

    return BitcoinRPC(
        "http://127.0.0.1:18443",
        "u",
        "p",
        client=httpx.Client(auth=httpx.BasicAuth("u", "p"), transport=httpx.MockTransport(handler)),
    )


def test_sync_stores_full_graph() -> None:
    db = open_memory_db()
    importer = BitcoinCoreOnchainImporter(_rpc(), db)
    summary = importer.sync("watch", known_addresses={OUR_ADDR})

    assert summary == {"txs_seen": 2, "new_txs": 2, "self_transfers": 1, "ownership_fixed": 0}
    assert db.execute("SELECT COUNT(*) FROM transactions").fetchone() == (2,)
    assert db.execute("SELECT COUNT(*) FROM tx_outputs").fetchone() == (3,)
    assert db.execute("SELECT COUNT(*) FROM tx_inputs").fetchone() == (1,)
    assert db.execute("SELECT is_coinbase FROM transactions WHERE txid='aaa'").fetchone() == (1,)
    assert db.execute("SELECT block_height FROM transactions WHERE txid='bbb'").fetchone() == (102,)
    assert db.execute(
        "SELECT value_sat FROM tx_outputs WHERE txid='bbb' AND vout=1"
    ).fetchone() == (524990000,)
    # spend linkage: coinbase output marked spent by bbb:0
    assert db.execute(
        "SELECT spent_by_txid, spent_by_input FROM tx_outputs WHERE txid='aaa'"
    ).fetchone() == ("bbb", 0)
    # ownership: external payment output proven external, change stays ours
    assert db.execute("SELECT is_mine FROM tx_outputs WHERE txid='bbb' AND vout=0").fetchone() == (
        0,
    )
    assert db.execute("SELECT is_mine FROM tx_outputs WHERE txid='bbb' AND vout=1").fetchone() == (
        1,
    )
    assert (
        db.execute("SELECT value FROM sync_state WHERE key='last_sync_at'").fetchone() is not None
    )


def test_sync_is_incremental() -> None:
    db = open_memory_db()
    importer = BitcoinCoreOnchainImporter(_rpc(), db)
    importer.sync("watch")
    summary = importer.sync("watch")
    assert summary == {"txs_seen": 2, "new_txs": 0, "self_transfers": 0, "ownership_fixed": 0}


def test_refresh_ownership_backfills_old_rows() -> None:
    """Pre-ownership DBs assumed everything ours; the refresh pass corrects that."""
    db = open_memory_db()
    importer = BitcoinCoreOnchainImporter(_rpc(), db)
    importer.sync("watch")
    # Simulate an old row: external output recorded as ours.
    db.execute(
        "UPDATE tx_outputs SET is_mine = 1 WHERE txid = 'bbb' AND vout = 0",
    )
    db.commit()
    assert importer.refresh_ownership() == 1
    assert db.execute("SELECT is_mine FROM tx_outputs WHERE txid='bbb' AND vout=0").fetchone() == (
        0,
    )
    assert importer.refresh_ownership() == 0  # second pass is a no-op


def test_reconcile_compares_db_against_node(capsys, tmp_path) -> None:
    import json
    import sqlite3
    from unittest.mock import patch

    import httpx

    from utxoproof.bitcoin_rpc import BitcoinRPC
    from utxoproof.cli import main

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        method = body["method"]
        if method == "getbalances":
            return httpx.Response(
                200,
                json={
                    "result": {
                        "mine": {"trusted": 5.2499, "untrusted_pending": 0},
                        "watchonly": {"trusted": 0, "untrusted_pending": 0},
                    },
                    "error": None,
                },
            )
        raise AssertionError(f"unexpected method {method}")

    rpc = BitcoinRPC(
        "http://127.0.0.1:18443",
        "u",
        "p",
        client=httpx.Client(auth=httpx.BasicAuth("u", "p"), transport=httpx.MockTransport(handler)),
    )
    db = open_memory_db()
    BitcoinCoreOnchainImporter(_rpc(), db).sync("watch")
    db_path = tmp_path / "r.db"
    dest = sqlite3.connect(str(db_path))
    db.backup(dest)
    dest.close()
    with patch("utxoproof.bitcoin_rpc.BitcoinRPC", lambda *a, **k: rpc):
        assert (
            main(
                [
                    "reconcile",
                    "--db",
                    str(db_path),
                    "--rpc-url",
                    "http://127.0.0.1:18443",
                    "--rpc-user",
                    "u",
                    "--rpc-password",
                    "p",
                    "--wallet",
                    "watch",
                ]
            )
            == 0
        )
    out = capsys.readouterr().out
    assert "RECONCILED" in out


def test_encompassing_range() -> None:
    from utxoproof.onchain import BitcoinCoreOnchainImporter

    assert BitcoinCoreOnchainImporter.encompassing_range(None) == (0, 1000)
    assert BitcoinCoreOnchainImporter.encompassing_range((0, 1000)) is None
    assert BitcoinCoreOnchainImporter.encompassing_range((0, 1002)) is None
    # Stored narrower range widens instead of being rejected by Core.
    assert BitcoinCoreOnchainImporter.encompassing_range((0, 10)) == (0, 1000)
    assert BitcoinCoreOnchainImporter.encompassing_range((5, 5000)) == (0, 5000)


def test_import_descriptor_skips_already_covered() -> None:
    import json

    import httpx

    from utxoproof.bitcoin_rpc import BitcoinRPC
    from utxoproof.db import open_memory_db

    calls: list[str] = []
    desc = (
        "wpkh([aabbccdd/84h/0h/0h]"
        "xpub661MyMwAqRbcFtXgS5sYJABqqG9YLmC4Q1Rdap9gSE8NqtwybGhePY2gZ29ESFjqJoCu1R59rh8C/0/*)"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        method = body["method"]
        calls.append(method)
        if method == "listdescriptors":
            return httpx.Response(
                200,
                json={
                    "result": {
                        "descriptors": [
                            {"desc": desc + "#0fujx7aa", "range": [0, 1002], "active": False}
                        ]
                    },
                    "error": None,
                },
            )
        raise AssertionError(f"importdescriptors must not be called, got {method}")

    rpc = BitcoinRPC(
        "http://127.0.0.1:18443",
        "u",
        "p",
        client=httpx.Client(auth=httpx.BasicAuth("u", "p"), transport=httpx.MockTransport(handler)),
    )
    importer = BitcoinCoreOnchainImporter(rpc, open_memory_db())
    assert importer.import_descriptor("watch", desc) is False
    assert calls == ["listdescriptors"]


COLD_ADDR = "bcrt1qcoldaddress0000000000000000000000000000"
HOT_ADDR = "bcrt1qhotaddress00000000000000000000000000000"


def _multi_rpc() -> BitcoinRPC:
    t1 = {
        "txid": "t1",
        "vin": [{"coinbase": "04ffff", "sequence": 0}],
        "vout": [{"value": 10.0, "n": 0, "scriptPubKey": {"address": COLD_ADDR}}],
        "blockhash": "block1",
        "blocktime": 1700000000,
    }
    t2 = {
        "txid": "t2",
        "vin": [{"txid": "t1", "vout": 0, "sequence": 0}],
        "vout": [
            {"value": 6.0, "n": 0, "scriptPubKey": {"address": HOT_ADDR}},
            {"value": 3.999, "n": 1, "scriptPubKey": {"address": EXT_ADDR}},
        ],
        "blockhash": "block2",
        "blocktime": 1700000600,
    }
    raws = {"t1": t1, "t2": t2}
    lists = {"cold": ["t1", "t2"], "hot": ["t2"]}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        body = json.loads(request.content)
        method = body["method"]
        wallet = "cold" if "/wallet/cold" in str(request.url) else "hot"
        if method == "listtransactions":
            return httpx.Response(
                200, json={"result": [{"txid": t} for t in lists[wallet]], "error": None}
            )
        if method == "getrawtransaction":
            return httpx.Response(200, json={"result": raws[body["params"][0]], "error": None})
        if method == "getblockheader":
            heights = {"block1": 101, "block2": 102}
            return httpx.Response(
                200, json={"result": {"height": heights[body["params"][0]]}, "error": None}
            )
        if method == "getaddressinfo":
            return httpx.Response(
                200,
                json={
                    "result": {"ismine": body["params"][0] in (COLD_ADDR, HOT_ADDR)},
                    "error": None,
                },
            )
        raise AssertionError(f"unexpected method {method}")

    return BitcoinRPC(
        "http://127.0.0.1:18443",
        "u",
        "p",
        client=httpx.Client(auth=httpx.BasicAuth("u", "p"), transport=httpx.MockTransport(handler)),
    )


def _labels(db) -> dict[str, str | None]:
    return {
        row[0]: row[1]
        for row in db.execute("SELECT txid || ':' || vout, wallet_label FROM tx_outputs").fetchall()
    }


def test_sync_attributes_wallets_including_shared_tx() -> None:
    db = open_memory_db()
    BitcoinCoreOnchainImporter(_multi_rpc(), db).sync("cold")
    BitcoinCoreOnchainImporter(_multi_rpc(), db).sync("hot")
    labels = _labels(db)
    assert labels["t1:0"] == "cold"
    assert sorted((labels["t2:0"] or "").split(",")) == ["cold", "hot"]
    assert sorted((labels["t2:1"] or "").split(",")) == ["cold", "hot"]
    # Spends still link globally: cold funding is spent by the shared tx.
    assert db.execute("SELECT spent_by_txid FROM tx_outputs WHERE txid='t1'").fetchone() == ("t2",)


def test_sync_attribution_is_idempotent() -> None:
    db = open_memory_db()
    imp = BitcoinCoreOnchainImporter(_multi_rpc(), db)
    imp.sync("cold")
    imp.sync("hot")
    before = _labels(db)
    imp.sync("cold")
    imp.sync("hot")
    assert _labels(db) == before


def test_reconcile_filters_by_wallet_and_flags_legacy_rows(capsys, tmp_path) -> None:
    import json
    import sqlite3
    from unittest.mock import patch

    import httpx

    from utxoproof.bitcoin_rpc import BitcoinRPC
    from utxoproof.cli import main

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["method"] == "getbalances"
        return httpx.Response(
            200,
            json={
                "result": {
                    "mine": {"trusted": 6.2499, "untrusted_pending": 0},
                    "watchonly": {"trusted": 0, "untrusted_pending": 0},
                },
                "error": None,
            },
        )

    rpc = BitcoinRPC(
        "http://127.0.0.1:18443",
        "u",
        "p",
        client=httpx.Client(auth=httpx.BasicAuth("u", "p"), transport=httpx.MockTransport(handler)),
    )
    db = open_memory_db()
    BitcoinCoreOnchainImporter(_rpc(), db).sync("watch")
    # Label the synced output cold; add one legacy unattributed row (1.0 BTC).
    db.execute("UPDATE tx_outputs SET wallet_label='cold' WHERE spent_by_txid IS NULL")
    db.execute("INSERT INTO transactions (txid, block_time) VALUES ('legacy', '2024-01-01')")
    db.execute(
        "INSERT INTO tx_outputs (txid, vout, address, value_sat, is_mine) "
        "VALUES ('legacy', 0, 'bcrt1qlegacy', 100000000, 1)"
    )
    db.commit()
    db_path = tmp_path / "r.db"
    dest = sqlite3.connect(str(db_path))
    db.backup(dest)
    dest.close()
    with patch("utxoproof.bitcoin_rpc.BitcoinRPC", lambda *a, **k: rpc):
        assert (
            main(
                [
                    "reconcile",
                    "--db",
                    str(db_path),
                    "--rpc-url",
                    "http://127.0.0.1:18443",
                    "--rpc-user",
                    "u",
                    "--rpc-password",
                    "p",
                    "--wallet",
                    "cold",
                ]
            )
            == 0
        )
    out = capsys.readouterr().out
    assert "RECONCILED" in out  # 5.2499 labeled + 1.0 legacy = 6.2499 node
    assert "legacy fallback" in out
