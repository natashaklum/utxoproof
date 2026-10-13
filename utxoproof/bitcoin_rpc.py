"""Minimal Bitcoin Core JSON-RPC client over httpx (plan Sec. 9).

Covers only what the on-chain importer needs; no `python-bitcoinrpc`
dependency. Wallet RPCs are addressed via the `/wallet/<name>` endpoint.
"""

from __future__ import annotations

from typing import Any

import httpx

JSON_RPC_URL = "http://127.0.0.1:18443"  # regtest default


class BitcoinRPCError(RuntimeError):
    pass


class BitcoinRPC:
    def __init__(
        self,
        url: str = JSON_RPC_URL,
        user: str = "",
        password: str = "",
        client: httpx.Client | None = None,
    ) -> None:
        self._url = url.rstrip("/")
        auth = httpx.BasicAuth(user, password) if user or password else None
        self._client = client or httpx.Client(auth=auth, timeout=60.0)

    def wallet(self, name: str) -> BitcoinRPC:
        """Return a client bound to the `/wallet/<name>` endpoint."""
        return BitcoinRPC(
            f"{self._url}/wallet/{name}",
            client=self._client,
        )

    def call(self, method: str, *params: Any) -> Any:
        """Single JSON-RPC call. Raises BitcoinRPCError on transport/RPC error.

        Note: bitcoind answers HTTP 500 for RPC-level errors, so the JSON
        body is inspected before the HTTP status.
        """
        try:
            response = self._client.post(
                self._url, json={"jsonrpc": "1.0", "method": method, "params": list(params)}
            )
        except httpx.HTTPError as exc:
            raise BitcoinRPCError(f"RPC {method} failed: {exc}") from exc
        try:
            payload = response.json()
        except ValueError:
            raise BitcoinRPCError(
                f"RPC {method} HTTP {response.status_code}: {response.text[:200]}"
            ) from None
        if payload.get("error"):
            err = payload["error"]
            raise BitcoinRPCError(f"RPC {method} error {err.get('code')}: {err.get('message')}")
        return payload.get("result")

    # -- convenience wrappers used by the importer -----------------------

    def get_block_count(self) -> int:
        return int(self.call("getblockcount"))

    def create_wallet(
        self, name: str, disable_private_keys: bool = True, blank: bool = True
    ) -> dict[str, Any]:
        result = self.call("createwallet", name, disable_private_keys, blank)
        return dict(result)

    def import_descriptors(self, wallet: str, descriptors: list[dict[str, Any]]) -> list[Any]:
        return list(self.wallet(wallet).call("importdescriptors", descriptors))

    def list_transactions(self, wallet: str, count: int = 1000) -> list[dict[str, Any]]:
        return list(self.wallet(wallet).call("listtransactions", "*", count))

    def getbalances(self, wallet: str) -> dict[str, Any]:
        """Wallet balances split into mine/watchonly buckets (BTC floats)."""
        return dict(self.wallet(wallet).call("getbalances"))

    def getaddressinfo(self, address: str) -> dict[str, Any]:
        """Address metadata; ``ismine`` tells whether any wallet owns it."""
        return dict(self.call("getaddressinfo", address))

    def get_raw_transaction(
        self, txid: str, verbose: bool = True, blockhash: str | None = None
    ) -> dict[str, Any]:
        """Fetch a tx. Without -txindex, confirmed txs need their blockhash
        (present on `listtransactions` entries); mempool txs need none."""
        params: list[Any] = [txid, verbose]
        if blockhash:
            params.append(blockhash)
        return dict(self.call("getrawtransaction", *params))

    def generate_to_address(self, blocks: int, address: str) -> list[str]:
        return list(self.call("generatetoaddress", blocks, address))

    def get_new_address(self, wallet: str) -> str:
        return str(self.wallet(wallet).call("getnewaddress"))

    def send_to_address(self, wallet: str, address: str, amount_btc: float) -> str:
        return str(self.wallet(wallet).call("sendtoaddress", address, amount_btc))
