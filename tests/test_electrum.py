"""Electrum client tests (fake in-process transport, no network)."""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest

from utxoproof.electrum import (
    ElectrumClient,
    ElectrumError,
    HistoryEntry,
    RawTransaction,
    fetch_transactions,
    get_histories,
    scripthash,
)

FIXTURE = Path(__file__).parent / "fixtures" / "electrum" / "block_100000.json"


class FakeTransport:
    def __init__(self, handler: Callable[[object], object]) -> None:
        self.handler = handler
        self.payloads: list[object] = []
        self.closed = False

    def request(self, payload: object) -> object:
        self.payloads.append(payload)
        return self.handler(payload)

    def close(self) -> None:
        self.closed = True


def _client(handler: Callable[[object], object]) -> tuple[ElectrumClient, FakeTransport]:
    fake = FakeTransport(handler)
    return ElectrumClient("ssl://electrum.example:50002", transport=fake), fake


def _fixture() -> dict[str, object]:
    return cast(dict[str, object], json.loads(FIXTURE.read_text(encoding="utf-8")))


def _fixture_transactions() -> dict[str, dict[str, object]]:
    fixture = _fixture()
    return {
        cast(str, tx["txid"]): cast(dict[str, object], tx)
        for tx in cast(list[dict[str, object]], fixture["transactions"])
    }


def test_scripthash_known_vector() -> None:
    script_hex = "76a914c398efa9c392ba6013c5e04ee729755ef7f58b3288ac"
    assert (
        scripthash(script_hex) == "9c2c84a6cf9809e08af19557e28d38257e6fee6981269760637a5f9dfb000b05"
    )

    # Independent check on a BIP84 witness program.
    witness_script = "0014751e76e8199196d454941c45d1b3a323f1433bd6"
    expected = hashlib.sha256(bytes.fromhex(witness_script)).digest()[::-1].hex()
    assert scripthash(witness_script) == expected


def test_scripthash_rejects_invalid_hex() -> None:
    with pytest.raises(ValueError, match="scriptPubKey"):
        scripthash("not-hex")


def test_url_defaults_for_ssl_and_tcp_without_connecting() -> None:
    fake = FakeTransport(lambda payload: {})
    ssl_client = ElectrumClient("ssl://electrum.example", transport=fake)
    assert ssl_client._use_ssl is True
    assert ssl_client._port == 50002

    tcp_client = ElectrumClient("tcp://electrum.example", transport=fake)
    assert tcp_client._use_ssl is False
    assert tcp_client._port == 50001

    explicit = ElectrumClient("tcp://electrum.example:1234", transport=fake)
    assert explicit._port == 1234


def test_url_rejects_unknown_scheme_and_missing_host() -> None:
    fake = FakeTransport(lambda payload: {})
    with pytest.raises(ValueError, match="scheme"):
        ElectrumClient("http://electrum.example", transport=fake)
    with pytest.raises(ValueError, match="host"):
        ElectrumClient("ssl://", transport=fake)


def test_call_sends_one_json_rpc_request() -> None:
    def handler(payload: object) -> object:
        request = cast(dict[str, object], payload)
        assert request["jsonrpc"] == "2.0"
        assert request["method"] == "server.version"
        assert request["params"] == ["utxoproof", "1.4"]
        return {"jsonrpc": "2.0", "id": request["id"], "result": ["ElectrumX 1.16", "1.4"]}

    client, fake = _client(handler)
    assert client.call("server.version", "utxoproof", "1.4") == ["ElectrumX 1.16", "1.4"]
    assert len(fake.payloads) == 1
    assert isinstance(fake.payloads[0], dict)


def test_call_propagates_rpc_error() -> None:
    def handler(payload: object) -> object:
        request = cast(dict[str, object], payload)
        return {
            "jsonrpc": "2.0",
            "id": request["id"],
            "error": {"code": -32601, "message": "unknown method"},
        }

    client, _ = _client(handler)
    with pytest.raises(ElectrumError, match="unknown method"):
        client.call("server.nope")


def test_batch_uses_one_payload_and_preserves_request_order() -> None:
    def handler(payload: object) -> object:
        requests = cast(list[dict[str, object]], payload)
        assert [request["method"] for request in requests] == ["method.one", "method.two"]
        replies = [
            {"jsonrpc": "2.0", "id": request["id"], "result": request["params"][0]}
            for request in requests
        ]
        return list(reversed(replies))  # id mapping must restore request order

    client, fake = _client(handler)
    assert client.batch([("method.one", ["first"]), ("method.two", ["second"])]) == [
        "first",
        "second",
    ]
    assert len(fake.payloads) == 1
    assert isinstance(fake.payloads[0], list)


def test_batch_raises_on_any_entry_error() -> None:
    def handler(payload: object) -> object:
        requests = cast(list[dict[str, object]], payload)
        return [
            {"jsonrpc": "2.0", "id": requests[0]["id"], "result": "ok"},
            {
                "jsonrpc": "2.0",
                "id": requests[1]["id"],
                "error": {"code": -5, "message": "no such transaction"},
            },
        ]

    client, _ = _client(handler)
    with pytest.raises(ElectrumError, match="no such transaction"):
        client.batch([("method.one", []), ("method.two", [])])


def test_context_manager_closes_transport_once() -> None:
    fake = FakeTransport(lambda payload: {})
    with ElectrumClient("tcp://electrum.example", transport=fake) as client:
        assert client._closed is False
    assert fake.closed is True
    assert client._closed is True


def test_get_histories_keys_and_mempool_height() -> None:
    scripts = {
        "receive/0": "76a914c398efa9c392ba6013c5e04ee729755ef7f58b3288ac",
        "change/0": "0014751e76e8199196d454941c45d1b3a323f1433bd6",
        "receive/0-copy": "76a914c398efa9c392ba6013c5e04ee729755ef7f58b3288ac",
    }
    receive_hash = scripthash(scripts["receive/0"])
    change_hash = scripthash(scripts["change/0"])

    def handler(payload: object) -> object:
        requests = cast(list[dict[str, object]], payload)
        replies = []
        for request in requests:
            assert request["method"] == "blockchain.scripthash.get_history"
            sh = cast(list[str], request["params"])[0]
            if sh == receive_hash:
                result = [
                    {"txid": "confirmed", "height": 700000},
                    {"txid": "mempool-zero", "height": 0},
                    {"txid": "mempool-parent", "height": -1},
                ]
            elif sh == change_hash:
                result = [{"txid": "change-tx", "height": 700001}]
            else:  # pragma: no cover - guards the fake itself
                raise AssertionError(f"unexpected scripthash {sh}")
            replies.append({"jsonrpc": "2.0", "id": request["id"], "result": result})
        return replies

    client, fake = _client(handler)
    histories = get_histories(client, scripts, batch_size=2)

    assert list(histories) == list(scripts)
    assert histories["receive/0"] == [
        HistoryEntry("confirmed", 700000),
        HistoryEntry("mempool-zero", 0),
        HistoryEntry("mempool-parent", 0),
    ]
    assert histories["change/0"] == [HistoryEntry("change-tx", 700001)]
    assert histories["receive/0-copy"] == histories["receive/0"]
    assert len(fake.payloads) == 2  # three scripts, batch_size=2


def test_get_histories_retries_when_a_public_server_drops_a_large_batch() -> None:
    scripts = {f"script-{i}": "0014" + "00" * 20 for i in range(3)}

    def handler(payload: object) -> object:
        if isinstance(payload, list) and len(payload) > 2:
            raise ElectrumError("Electrum server closed the connection")
        requests = cast(list[dict[str, object]], payload)
        return [{"jsonrpc": "2.0", "id": request["id"], "result": []} for request in requests]

    client, fake = _client(handler)
    assert get_histories(client, scripts) == {key: [] for key in scripts}
    assert [len(cast(list[object], payload)) for payload in fake.payloads] == [3, 1, 2]


def test_fetch_transactions_decodes_real_transaction_and_dedupes() -> None:
    fixture = _fixture()
    txs = _fixture_transactions()
    confirmed_id = "fff2525b8931402dd09222c50775608f75787bd2b87e56995a7bdd30f79702c4"
    mempool_id = "8c14f0db3df150123e6f3dbbf30f8b955a8249b62ac1d1ff16284aefa3d06d87"
    header_hex = cast(str, fixture["header_hex"])
    block_time = cast(int, fixture["block_time"])
    requested_txids: list[str] = []

    def handler(payload: object) -> object:
        requests = cast(list[dict[str, object]], payload)
        replies = []
        for request in requests:
            method = request["method"]
            if method == "blockchain.transaction.get":
                txid = cast(list[str], request["params"])[0]
                requested_txids.append(txid)
                result: object = txs[txid]["raw_hex"]
            elif method == "blockchain.block.header":
                assert request["params"] == [100000]
                result = header_hex
            else:  # pragma: no cover - guards the fake itself
                raise AssertionError(f"unexpected method {method}")
            replies.append({"jsonrpc": "2.0", "id": request["id"], "result": result})
        return replies

    client, _ = _client(handler)
    before = int(time.time())
    entries = {
        "receive/0": [
            HistoryEntry(confirmed_id, 100000),
            HistoryEntry(mempool_id, 0),
        ],
        "change/0": [HistoryEntry(confirmed_id, 100000)],
    }
    fetched = fetch_transactions(client, entries)
    after = int(time.time())

    assert sorted(requested_txids) == sorted([confirmed_id, mempool_id])
    assert set(fetched) == {confirmed_id, mempool_id}
    assert isinstance(fetched[confirmed_id], RawTransaction)

    confirmed = fetched[confirmed_id]
    assert confirmed.txid == confirmed_id
    assert confirmed.height == 100000
    assert confirmed.block_time == block_time == 1293623863
    assert confirmed.raw["txid"] == confirmed_id
    assert confirmed.raw["version"] == 1
    assert confirmed.raw["locktime"] == 0
    assert confirmed.raw["size"] == 259
    assert confirmed.raw["blocktime"] == block_time
    assert confirmed.raw["time"] == block_time

    vin = cast(list[dict[str, object]], confirmed.raw["vin"])
    assert len(vin) == 1
    assert vin[0]["txid"] == "87a157f3fd88ac7907c05fc55e271dc4acdc5605d187d646604ca8c0e9382e03"
    assert vin[0]["vout"] == 0
    assert vin[0]["sequence"] == 0xFFFFFFFF
    assert cast(dict[str, str], vin[0]["scriptSig"])["hex"].startswith("493046022100")

    vout = cast(list[dict[str, object]], confirmed.raw["vout"])
    assert vout == [
        {
            "n": 0,
            "value": "5.56000000",
            "scriptPubKey": {
                "hex": "76a914c398efa9c392ba6013c5e04ee729755ef7f58b3288ac",
                "address": "1JqDybm2nWTENrHvMyafbSXXtTk5Uv5QAn",
            },
        },
        {
            "n": 1,
            "value": "44.44000000",
            "scriptPubKey": {
                "hex": "76a914948c765a6914d43f2a7ac177da2c2f6b52de3d7c88ac",
                "address": "1EYTGtG4LnFfiMvjJdsU7GMGCQvsRSjYhx",
            },
        },
    ]

    mempool = fetched[mempool_id]
    assert mempool.height == 0
    assert before <= mempool.block_time <= after
    assert mempool.raw["blocktime"] == mempool.block_time


def test_fetch_transactions_coinbase_input_and_unaddressable_output() -> None:
    fixture = _fixture()
    txs = _fixture_transactions()
    txid = "8c14f0db3df150123e6f3dbbf30f8b955a8249b62ac1d1ff16284aefa3d06d87"

    def handler(payload: object) -> object:
        requests = cast(list[dict[str, object]], payload)
        replies = []
        for request in requests:
            if request["method"] == "blockchain.transaction.get":
                result: object = txs[txid]["raw_hex"]
            else:
                assert request["method"] == "blockchain.block.header"
                result = fixture["header_hex"]
            replies.append({"jsonrpc": "2.0", "id": request["id"], "result": result})
        return replies

    client, _ = _client(handler)
    fetched = fetch_transactions(client, {"coinbase": [HistoryEntry(txid, 100000)]})
    coinbase = fetched[txid]

    vin = cast(list[dict[str, object]], coinbase.raw["vin"])
    assert vin == [{"coinbase": "044c86041b020602", "sequence": 0xFFFFFFFF}]
    vout = cast(list[dict[str, object]], coinbase.raw["vout"])
    assert vout[0]["value"] == "50.00000000"
    script_pubkey = cast(dict[str, object], vout[0]["scriptPubKey"])
    assert script_pubkey["address"] is None
    assert script_pubkey["hex"] == (
        "41041b0e8c2567c12536aa13357b79a073dc4444acb83c4ec7a0e2f99dd7457516"
        "c5817242da796924ca4e99947d087fedf9ce467cb9f7c6287078f801df276fdf84ac"
    )


def test_fetch_transactions_wraps_bad_raw_hex() -> None:
    def handler(payload: object) -> object:
        requests = cast(list[dict[str, object]], payload)
        return [
            {"jsonrpc": "2.0", "id": request["id"], "result": "not hex"} for request in requests
        ]

    client, _ = _client(handler)
    with pytest.raises(ElectrumError, match="Invalid raw transaction"):
        fetch_transactions(client, {"script": [HistoryEntry("deadbeef", 0)]})


def test_batch_size_must_be_positive() -> None:
    client, _ = _client(lambda payload: [])
    with pytest.raises(ValueError, match="batch_size"):
        get_histories(client, {}, batch_size=0)
    with pytest.raises(ValueError, match="batch_size"):
        fetch_transactions(client, {}, batch_size=0)
