"""On-chain importer skeleton (plan Sec. 9, Sprint 3 start).

Stores the full watched tx graph (transactions, tx_inputs, tx_outputs) in the
Sec. 8 SQLite schema. Covered: watch-only wallet setup, descriptor import,
incremental sync from `listtransactions` with `getrawtransaction` resolution,
spend linkage, self-transfer counting. Explicitly pending (full Sprint 3):
block-height backfill tracking, rescan orchestration, BIP329 labels.
"""

from __future__ import annotations

import datetime
import sqlite3
from decimal import Decimal
from typing import Any

from utxoproof.bitcoin_rpc import BitcoinRPC, BitcoinRPCError
from utxoproof.descriptors import with_checksum

SAT_PER_BTC = 100_000_000


def _btc_to_sat(value: float | str) -> int:
    return int(Decimal(str(value)) * SAT_PER_BTC)


def _iso(ts: float | int) -> str:
    return datetime.datetime.fromtimestamp(int(ts), datetime.UTC).isoformat()


class BitcoinCoreOnchainImporter:
    def __init__(self, rpc: BitcoinRPC, db: sqlite3.Connection) -> None:
        self._rpc = rpc
        self._db = db
        self._height_cache: dict[str, int] = {}
        self._mine_cache: dict[str, bool] = {}

    # -- setup -----------------------------------------------------------

    def setup_wallet(self, wallet_name: str) -> None:
        """Create a blank watch-only wallet; tolerate an existing one."""
        try:
            self._rpc.create_wallet(wallet_name, disable_private_keys=True, blank=True)
        except BitcoinRPCError as exc:
            if "already exists" not in str(exc):
                raise

    WANT_RANGE: tuple[int, int] = (0, 1000)

    @staticmethod
    def encompassing_range(have: tuple[int, int] | None) -> tuple[int, int] | None:
        """Range to import given the stored one (None = already covered, skip).

        Core rejects re-imports whose range does not include the stored range,
        so re-runs must widen, never narrow.
        """
        want_begin, want_end = BitcoinCoreOnchainImporter.WANT_RANGE
        if have is None:
            return (want_begin, want_end)
        if have[0] <= want_begin and have[1] >= want_end:
            return None
        return (min(have[0], want_begin), max(have[1], want_end))

    def import_descriptor(self, wallet: str, descriptor: str, timestamp: int | str = "now") -> bool:
        """Import one ranged descriptor without rescan orchestration (Sprint 3).

        Idempotent across re-runs: an already-covered descriptor is skipped,
        a narrower stored range is widened. Returns True when imported.
        Appends the Core-required `#checksum` suffix when absent.
        """
        core = with_checksum(descriptor).split("#")[0]
        have: tuple[int, int] | None = None
        try:
            for entry in self._rpc.listdescriptors(wallet):
                if str(entry.get("desc", "")).split("#")[0] != core:
                    continue
                stored = entry.get("range") or [0, 0]
                have = (int(stored[0]), int(stored[1]))
        except BitcoinRPCError:
            have = None  # old node or fresh wallet: fall back to plain import
        want = self.encompassing_range(have)
        if want is None:
            return False
        results = self._rpc.import_descriptors(
            wallet,
            [
                {
                    "desc": with_checksum(descriptor),
                    "timestamp": timestamp,
                    "range": [want[0], want[1]],
                    "active": False,
                }
            ],
        )
        failures = [r for r in results if not r.get("success")]
        if failures:
            raise BitcoinRPCError(f"importdescriptors rejected {descriptor}: {failures}")
        return True

    # -- sync ------------------------------------------------------------

    def sync(self, wallet: str, known_addresses: set[str] | None = None) -> dict[str, int]:
        """Import all wallet transactions not yet in the DB. Returns a summary."""
        seen = {row[0] for row in self._db.execute("SELECT txid FROM transactions").fetchall()}
        summary = {"txs_seen": 0, "new_txs": 0, "self_transfers": 0}
        self._mine_cache = {}
        for entry in self._rpc.list_transactions(wallet):
            summary["txs_seen"] += 1
            txid = str(entry["txid"])
            if txid in seen:
                self._attribute_tx(txid, wallet)
                continue
            raw = self._rpc.get_raw_transaction(txid, True, entry.get("blockhash"))
            self_transfer = self._store_tx(raw, known_addresses, wallet)
            summary["new_txs"] += 1
            summary["self_transfers"] += int(self_transfer)
            seen.add(txid)
        summary["ownership_fixed"] = self.refresh_ownership()
        self._db.execute(
            "INSERT OR REPLACE INTO sync_state (key, value, updated_at) VALUES (?,?,?)",
            (
                "last_sync_at",
                datetime.datetime.now(datetime.UTC).isoformat(),
                datetime.datetime.now(datetime.UTC).isoformat(),
            ),
        )
        self._db.commit()
        return summary

    # -- storage ----------------------------------------------------------

    def _is_mine(self, address: str | None) -> bool:
        """Whether an output address belongs to any wallet (cached per run).

        Address-less outputs (OP_RETURN, bare scripts) are never ours.
        RPC failures fail open (assumed ours) to preserve pre-ownership syncs.
        """
        if not address:
            return False
        if address not in self._mine_cache:
            try:
                self._mine_cache[address] = bool(
                    self._rpc.getaddressinfo(address).get("ismine", False)
                )
            except BitcoinRPCError:
                self._mine_cache[address] = True
        return self._mine_cache[address]

    def refresh_ownership(self) -> int:
        """Re-check ownership of every unspent output. Returns rows corrected to external."""
        fixed = 0
        rows = self._db.execute(
            "SELECT txid, vout, address, is_mine FROM tx_outputs WHERE spent_by_txid IS NULL"
        ).fetchall()
        for txid, vout, address, is_mine in rows:
            mine = self._is_mine(address)
            if bool(is_mine) != mine:
                self._db.execute(
                    "UPDATE tx_outputs SET is_mine=? WHERE txid=? AND vout=?",
                    (int(mine), txid, vout),
                )
                fixed += int(not mine)
        self._db.commit()
        return fixed

    def _attribute_tx(self, txid: str, wallet: str) -> None:
        """Record that ``wallet`` sees ``txid`` (idempotent, appends).

        Shared transactions (e.g. cold→hot self-transfers) accumulate every
        wallet that lists them, so each wallet's sync backfills attribution
        for rows stored before multi-wallet existed. Committed by the caller.
        """
        self._db.execute(
            "UPDATE tx_outputs SET wallet_label = CASE "
            "WHEN wallet_label IS NULL OR wallet_label = '' THEN ? "
            "WHEN instr(',' || wallet_label || ',', ',' || ? || ',') = 0 "
            "THEN wallet_label || ',' || ? ELSE wallet_label END "
            "WHERE txid = ?",
            (wallet, wallet, wallet, txid),
        )

    def _block_height(self, blockhash: str | None) -> int | None:
        if not blockhash:
            return None
        if blockhash not in self._height_cache:
            header = self._rpc.call("getblockheader", blockhash)
            self._height_cache[blockhash] = int(header["height"])
        return self._height_cache[blockhash]

    def _store_tx(
        self, raw: dict[str, Any], known_addresses: set[str] | None, wallet: str | None = None
    ) -> bool:
        txid = str(raw["txid"])
        is_coinbase = any("coinbase" in vin for vin in raw.get("vin", []))
        block_time = raw.get("blocktime", raw.get("time", 0))
        self._db.execute(
            "INSERT OR IGNORE INTO transactions "
            "(txid, block_height, block_time, is_coinbase) VALUES (?,?,?,?)",
            (txid, self._block_height(raw.get("blockhash")), _iso(block_time), int(is_coinbase)),
        )
        for index, vin in enumerate(raw.get("vin", [])):
            if "coinbase" in vin:
                continue
            self._db.execute(
                "INSERT OR IGNORE INTO tx_inputs "
                "(txid, input_index, prev_txid, prev_vout) VALUES (?,?,?,?)",
                (txid, index, str(vin["txid"]), int(vin["vout"])),
            )
            self._db.execute(
                "UPDATE tx_outputs SET spent_by_txid=?, spent_by_input=? WHERE txid=? AND vout=?",
                (txid, index, str(vin["txid"]), int(vin["vout"])),
            )
        addresses: list[str | None] = []
        for vout in raw.get("vout", []):
            address = vout.get("scriptPubKey", {}).get("address")
            addresses.append(address)
            self._db.execute(
                "INSERT OR IGNORE INTO tx_outputs (txid, vout, address, value_sat, is_mine) "
                "VALUES (?,?,?,?,?)",
                (
                    txid,
                    int(vout["n"]),
                    address,
                    _btc_to_sat(vout["value"]),
                    int(self._is_mine(address)),
                ),
            )
        if wallet is not None:
            self._attribute_tx(txid, wallet)
        self._db.commit()
        if known_addresses is None or not addresses:
            return False
        return all(addr in known_addresses for addr in addresses)
