"""bitcoin_rpc unit tests (mocked transport)."""

import httpx
import pytest

from utxoproof.bitcoin_rpc import BitcoinRPC, BitcoinRPCError


def _client(handler) -> BitcoinRPC:
    mock = httpx.Client(
        auth=httpx.BasicAuth("user", "pass"),
        transport=httpx.MockTransport(handler),
    )
    return BitcoinRPC("http://127.0.0.1:18443", "user", "pass", client=mock)


def test_call_returns_result() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        import json

        body = json.loads(request.content)
        assert body["method"] == "getblockcount"
        assert request.headers["authorization"].startswith("Basic ")
        return httpx.Response(200, json={"result": 150, "error": None, "id": 1})

    assert _client(handler).get_block_count() == 150


def test_rpc_error_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"result": None, "error": {"code": -4, "message": "oops"}, "id": 1}
        )

    with pytest.raises(BitcoinRPCError, match="oops"):
        _client(handler).call("getblockcount")


def test_http_500_with_json_error_surfaces_rpc_message() -> None:
    # bitcoind answers HTTP 500 for RPC-level errors (e.g. wallet exists);
    # the code/message must survive, not the HTTP status text.

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            500,
            json={
                "result": None,
                "error": {"code": -4, "message": "Wallet foo already exists"},
                "id": 1,
            },
        )

    with pytest.raises(BitcoinRPCError, match="already exists"):
        _client(handler).call("createwallet")


def test_wallet_endpoint() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json={"result": [], "error": None, "id": 1})

    _client(handler).list_transactions("watch")
    assert seen[0].endswith("/wallet/watch")


def test_list_all_transactions_paginates_and_includes_watchonly() -> None:
    calls: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        body = json.loads(request.content)
        calls.append(body)
        skip = body["params"][2]
        page = [{"txid": f"tx{skip + index}"} for index in range(2 if skip == 0 else 1)]
        return httpx.Response(200, json={"result": page, "error": None, "id": 1})

    client = _client(handler)
    entries = client.list_all_transactions("watch", page_size=2)
    assert [entry["txid"] for entry in entries] == ["tx0", "tx1", "tx2"]
    assert [call["params"] for call in calls] == [
        ["*", 2, 0, True],
        ["*", 2, 2, True],
    ]
