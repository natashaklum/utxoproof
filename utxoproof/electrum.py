"""Minimal pure-Python Electrum server client (plan Sec. 9).

Speaks the line-delimited JSON-RPC protocol used by Electrum servers over
``ssl://`` (default port 50002) or ``tcp://`` (50001). TLS verification uses
the system trust store by default; pass ``ca_file`` for a private/self-signed
certificate. The network is never touched at construction time, and an
optional ``transport`` makes the client testable without sockets.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import socket
import ssl
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from types import TracebackType
from typing import BinaryIO, Protocol, TypeVar, cast
from urllib.parse import urlsplit

from embit import networks
from embit.base import EmbitError
from embit.transaction import Transaction, TransactionInput

SAT_PER_BTC = Decimal(100_000_000)
DEFAULT_SSL_PORT = 50002
DEFAULT_TCP_PORT = 50001
BATCH_SIZE = 100


class ElectrumError(RuntimeError):
    """Transport, protocol, or malformed-response failure."""


class _Transport(Protocol):
    def request(self, payload: object) -> object: ...

    def close(self) -> None: ...


@dataclass
class HistoryEntry:
    txid: str
    height: int


@dataclass
class RawTransaction:
    txid: str
    raw: dict[str, object]
    height: int
    block_time: int


def scripthash(script_pubkey_hex: str) -> str:
    """Electrum script hash: SHA256(script) in reversed (display) byte order."""
    try:
        script = bytes.fromhex(script_pubkey_hex)
    except ValueError as exc:
        raise ValueError(f"Invalid scriptPubKey hex: {script_pubkey_hex!r}") from exc
    return hashlib.sha256(script).digest()[::-1].hex()


def _parse_url(url: str) -> tuple[str, int, bool]:
    parts = urlsplit(url)
    if parts.scheme not in ("ssl", "tcp"):
        raise ValueError(f"Unsupported Electrum URL scheme {parts.scheme!r}; use ssl:// or tcp://")
    host = parts.hostname
    if not host:
        raise ValueError(f"Electrum URL has no host: {url!r}")
    use_ssl = parts.scheme == "ssl"
    default_port = DEFAULT_SSL_PORT if use_ssl else DEFAULT_TCP_PORT
    try:
        port = parts.port
    except ValueError as exc:
        raise ValueError(f"Invalid Electrum URL port: {url!r}") from exc
    return host, port if port is not None else default_port, use_ssl


def _error_text(error: object) -> str:
    if isinstance(error, dict):
        code = error.get("code")
        message = error.get("message")
        if message is not None:
            return f"{code}: {message}" if code is not None else str(message)
    return str(error)


def _response_dict(response: object, context: str) -> dict[str, object]:
    if not isinstance(response, dict):
        raise ElectrumError(f"{context}: expected a JSON object, got {type(response).__name__}")
    return {str(key): value for key, value in response.items()}


def _result(payload: dict[str, object], method: str) -> object:
    error = payload.get("error")
    if error is not None:
        raise ElectrumError(f"RPC {method} failed: {_error_text(error)}")
    if "result" not in payload:
        raise ElectrumError(f"RPC {method}: response has no result")
    return payload["result"]


class _SocketTransport:
    """Line-delimited JSON-RPC over a lazily connected TCP/TLS socket."""

    def __init__(
        self,
        host: str,
        port: int,
        use_ssl: bool,
        ca_file: str | None,
        timeout: float,
    ) -> None:
        self._host = host
        self._port = port
        self._use_ssl = use_ssl
        self._ca_file = ca_file
        self._timeout = timeout
        self._sock: socket.socket | None = None
        self._reader: BinaryIO | None = None
        self._writer: BinaryIO | None = None

    def _connection(self) -> tuple[BinaryIO, BinaryIO]:
        if self._reader is not None and self._writer is not None:
            return self._reader, self._writer
        try:
            raw = socket.create_connection((self._host, self._port), timeout=self._timeout)
        except OSError as exc:
            raise ElectrumError(
                f"Electrum connect to {self._host}:{self._port} failed: {exc}"
            ) from exc
        try:
            if self._use_ssl:
                # Default context verifies the certificate chain and hostname.
                # A ca_file adds/replaces trust anchors (needed for self-signed
                # servers); verification is never disabled silently.
                context = ssl.create_default_context(cafile=self._ca_file)
                sock: socket.socket = context.wrap_socket(raw, server_hostname=self._host)
            else:
                sock = raw
            reader = sock.makefile("rb")
            writer = sock.makefile("wb")
        except (OSError, ssl.SSLError) as exc:
            raw.close()
            raise ElectrumError(f"Electrum TLS setup failed: {exc}") from exc
        self._sock = sock
        self._reader = reader
        self._writer = writer
        return reader, writer

    def request(self, payload: object) -> object:
        try:
            line = json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\n"
        except (TypeError, ValueError) as exc:
            raise ElectrumError(f"Electrum request is not JSON serializable: {exc}") from exc
        reader, writer = self._connection()
        try:
            writer.write(line)
            writer.flush()
            response_line = reader.readline()
        except (OSError, ValueError) as exc:
            self.close()
            raise ElectrumError(f"Electrum transport error: {exc}") from exc
        if not response_line:
            self.close()
            raise ElectrumError("Electrum server closed the connection")
        try:
            response: object = json.loads(response_line)
        except json.JSONDecodeError as exc:
            self.close()
            raise ElectrumError(f"Electrum server returned invalid JSON: {exc}") from exc
        return response

    def close(self) -> None:
        for handle in (self._reader, self._writer):
            if handle is not None:
                try:
                    handle.close()
                except OSError:
                    pass
        self._reader = None
        self._writer = None
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None


class ElectrumClient:
    def __init__(
        self,
        url: str,
        ca_file: str | None = None,
        timeout: float = 30.0,
        transport: object | None = None,
    ) -> None:
        self._url = url
        self._host, self._port, self._use_ssl = _parse_url(url)
        self._ca_file = ca_file
        self._timeout = timeout
        self._transport: _Transport = (
            cast(_Transport, transport)
            if transport is not None
            else _SocketTransport(self._host, self._port, self._use_ssl, ca_file, timeout)
        )
        self._next_id = 1
        self._closed = False

    # -- JSON-RPC --------------------------------------------------------

    def _new_request(self, method: str, params: list[object]) -> dict[str, object]:
        request_id = self._next_id
        self._next_id += 1
        return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}

    def _send(self, payload: object) -> object:
        if self._closed:
            raise ElectrumError("Electrum client is closed")
        try:
            return self._transport.request(payload)
        except ElectrumError:
            raise
        except Exception as exc:
            raise ElectrumError(f"Electrum transport failure: {exc}") from exc

    def call(self, method: str, *params: object) -> object:
        response = self._send(self._new_request(method, list(params)))
        return _result(_response_dict(response, f"RPC {method}"), method)

    def batch(self, calls: list[tuple[str, list[object]]]) -> list[object]:
        if not calls:
            return []
        requests: list[dict[str, object]] = []
        ids: list[int] = []
        methods: list[str] = []
        for method, params in calls:
            request = self._new_request(method, list(params))
            ids.append(cast(int, request["id"]))
            methods.append(method)
            requests.append(request)
        response = self._send(requests)
        if not isinstance(response, list):
            # Some servers answer a whole-batch failure with a single object.
            failure = _response_dict(response, "RPC batch")
            error = failure.get("error")
            if error is not None:
                raise ElectrumError(f"RPC batch failed: {_error_text(error)}")
            raise ElectrumError("RPC batch: expected a JSON array response")

        items: list[dict[str, object]] = []
        for item in response:
            if not isinstance(item, dict):
                raise ElectrumError("RPC batch: response entry is not a JSON object")
            items.append({str(key): value for key, value in item.items()})
        by_id = {item.get("id"): item for item in items if "id" in item}
        if all(request_id in by_id for request_id in ids):
            ordered = [by_id[request_id] for request_id in ids]
        elif len(items) == len(ids):
            # Tolerate servers/fakes that omit ids; JSON-RPC batch order is
            # commonly preserved, but ids win whenever they are available.
            ordered = items
        else:
            raise ElectrumError(f"RPC batch: got {len(items)} responses for {len(ids)} requests")
        return [_result(item, method) for item, method in zip(ordered, methods, strict=True)]

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._transport.close()
        except Exception as exc:
            raise ElectrumError(f"Electrum close failed: {exc}") from exc

    def __enter__(self) -> ElectrumClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


# -- high-level helpers --------------------------------------------------


T = TypeVar("T")


def _chunks(items: Sequence[T], size: int) -> Iterable[Sequence[T]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def get_histories(
    client: ElectrumClient,
    scripts: dict[str, str],
    batch_size: int = BATCH_SIZE,
) -> dict[str, list[HistoryEntry]]:
    """Fetch per-script histories, keyed by the caller's script ids."""
    if batch_size < 1:
        raise ValueError("batch_size must be >= 1")
    script_ids = list(scripts)
    histories: dict[str, list[HistoryEntry]] = {}
    for chunk in _chunks(script_ids, batch_size):
        calls: list[tuple[str, list[object]]] = [
            ("blockchain.scripthash.get_history", [scripthash(scripts[script_id])])
            for script_id in chunk
        ]
        results = client.batch(calls)
        for script_id, result in zip(chunk, results, strict=True):
            histories[script_id] = _history_entries(result, "blockchain.scripthash.get_history")
    return histories


def _history_entries(result: object, method: str) -> list[HistoryEntry]:
    if not isinstance(result, list):
        raise ElectrumError(f"RPC {method}: expected a history array")
    entries: list[HistoryEntry] = []
    for item in result:
        if not isinstance(item, dict):
            raise ElectrumError(f"RPC {method}: history entry is not an object")
        txid = item.get("txid")
        height = item.get("height")
        if not isinstance(txid, str) or not isinstance(height, int):
            raise ElectrumError(f"RPC {method}: malformed history entry {item!r}")
        entries.append(HistoryEntry(txid=txid, height=max(0, height)))
    return entries


def _btc_value(satoshis: int) -> str:
    return f"{Decimal(satoshis) / SAT_PER_BTC:.8f}"


def _is_coinbase_input(txin: TransactionInput) -> bool:
    return bool(txin.txid == b"\x00" * 32 and txin.vout == 0xFFFFFFFF)


def _decode_transaction(raw_hex: str) -> dict[str, object]:
    try:
        raw_bytes = bytes.fromhex(raw_hex)
        tx = Transaction.parse(raw_bytes)
    except (ValueError, EmbitError, IndexError) as exc:
        raise ElectrumError(f"Invalid raw transaction hex: {exc}") from exc

    vin: list[dict[str, object]] = []
    for txin in tx.vin:
        if _is_coinbase_input(txin):
            vin.append(
                {
                    "coinbase": txin.script_sig.data.hex(),
                    "sequence": txin.sequence,
                }
            )
            continue
        entry: dict[str, object] = {
            "txid": txin.txid.hex(),
            "vout": txin.vout,
            "scriptSig": {"hex": txin.script_sig.data.hex()},
            "sequence": txin.sequence,
        }
        witness = [item.hex() for item in txin.witness.items]
        if witness:
            entry["txinwitness"] = witness
        vin.append(entry)

    vout: list[dict[str, object]] = []
    for index, txout in enumerate(tx.vout):
        script_hex = txout.script_pubkey.data.hex()
        try:
            address: str | None = txout.script_pubkey.address(networks.NETWORKS["main"])
        except (ValueError, IndexError, EmbitError):
            address = None
        vout.append(
            {
                "n": index,
                "value": _btc_value(txout.value),
                "scriptPubKey": {"hex": script_hex, "address": address},
            }
        )

    return {
        "txid": tx.txid().hex(),
        "hash": tx.txid().hex(),
        "version": tx.version,
        "locktime": tx.locktime,
        "size": len(raw_bytes),
        "vin": vin,
        "vout": vout,
        "hex": raw_hex,
    }


def _header_timestamp(header_hex: str) -> int:
    try:
        header = bytes.fromhex(header_hex)
    except ValueError as exc:
        raise ElectrumError(f"Invalid block header hex: {exc}") from exc
    if len(header) < 72:
        raise ElectrumError(f"Block header is too short: {len(header)} bytes")
    return int.from_bytes(header[68:72], "little")


def fetch_transactions(
    client: ElectrumClient,
    entries: dict[str, list[HistoryEntry]],
    batch_size: int = BATCH_SIZE,
) -> dict[str, RawTransaction]:
    """Fetch and decode every unique txid in ``entries``."""
    if batch_size < 1:
        raise ValueError("batch_size must be >= 1")
    heights: dict[str, int] = {}
    for history in entries.values():
        for entry in history:
            height = max(0, entry.height)
            if entry.txid not in heights or (heights[entry.txid] == 0 and height > 0):
                heights[entry.txid] = height
    txids = list(heights)
    if not txids:
        return {}

    raw_by_txid: dict[str, str] = {}
    for tx_chunk in _chunks(txids, batch_size):
        tx_calls: list[tuple[str, list[object]]] = [
            ("blockchain.transaction.get", [txid]) for txid in tx_chunk
        ]
        tx_results = client.batch(tx_calls)
        for txid, tx_result in zip(tx_chunk, tx_results, strict=True):
            if not isinstance(txid, str) or not isinstance(tx_result, str):
                raise ElectrumError(f"RPC blockchain.transaction.get: malformed result for {txid}")
            raw_by_txid[txid] = tx_result

    header_times: dict[int, int] = {}
    confirmed_heights = sorted({height for height in heights.values() if height > 0})
    for height_chunk in _chunks(confirmed_heights, batch_size):
        header_calls: list[tuple[str, list[object]]] = [
            ("blockchain.block.header", [height]) for height in height_chunk
        ]
        header_results = client.batch(header_calls)
        for height, header_result in zip(height_chunk, header_results, strict=True):
            if not isinstance(height, int) or not isinstance(header_result, str):
                raise ElectrumError(f"RPC blockchain.block.header: malformed result for {height}")
            header_times[height] = _header_timestamp(header_result)

    now = int(datetime.datetime.now(datetime.UTC).timestamp())
    transactions: dict[str, RawTransaction] = {}
    for txid in txids:
        height = heights[txid]
        block_time = now if height == 0 else header_times[height]
        raw = _decode_transaction(raw_by_txid[txid])
        raw["blocktime"] = block_time
        raw["time"] = block_time
        transactions[txid] = RawTransaction(
            txid=txid, raw=raw, height=height, block_time=block_time
        )
    return transactions
