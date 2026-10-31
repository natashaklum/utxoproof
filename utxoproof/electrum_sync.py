"""Electrum-backed on-chain sync (plan v8).

Derives the account script registry, watches it through a self-hosted Electrum
server, extends the gap limit when activity reaches its edge, and stores the
same transaction graph as the Bitcoin Core importer. Electrum is the primary
backend because it needs no Core wallet imports and scales to many accounts.
"""

from __future__ import annotations

import datetime
import sqlite3
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, cast

from utxoproof.accounts import Account
from utxoproof.descriptors import derive_descriptor_scripts
from utxoproof.electrum import (
    ElectrumClient,
    HistoryEntry,
    RawTransaction,
    fetch_transactions,
    get_histories,
)

SAT_PER_BTC = Decimal(100_000_000)
DEFAULT_BATCH_SIZE = 100
MAX_GAP = 10_000


def _iso(ts: int) -> str:
    return datetime.datetime.fromtimestamp(int(ts), datetime.UTC).isoformat()


def _btc_to_sat(value: object) -> int:
    return int(Decimal(str(value)) * SAT_PER_BTC)


@dataclass
class _Script:
    account_id: str
    chain: int
    index: int
    address: str
    script_hex: str

    @property
    def key(self) -> str:
        return f"{self.account_id}|{self.chain}|{self.index}"


@dataclass
class ElectrumSyncSummary:
    accounts: int = 0
    scripts: int = 0
    txs_seen: int = 0
    new_txs: int = 0
    outputs_attributed: int = 0
    gap_extended: int = 0
    last_height: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "accounts": self.accounts,
            "scripts": self.scripts,
            "txs_seen": self.txs_seen,
            "new_txs": self.new_txs,
            "outputs_attributed": self.outputs_attributed,
            "gap_extended": self.gap_extended,
            "last_height": self.last_height,
        }


class ElectrumOnchainImporter:
    def __init__(self, client: ElectrumClient, db: sqlite3.Connection) -> None:
        self._client = client
        self._db = db
        self._scripts: dict[str, _Script] = {}
        self._script_owner: dict[str, str] = {}
        self._address_owner: dict[str, str] = {}

    # -- gap-limit discovery ---------------------------------------------

    def _derive(self, account: Account, chain: int, start: int, count: int) -> list[_Script]:
        descriptor = account.descriptors["external" if chain == 0 else "change"]
        pairs = derive_descriptor_scripts(
            descriptor,
            branch=0,
            start=start,
            count=count,
            network=account.network,
        )
        return [
            _Script(account.id, chain, start + offset, address, script_hex)
            for offset, (address, script_hex) in enumerate(pairs)
        ]

    def _discover_scripts(
        self, account: Account, histories: dict[str, list[HistoryEntry]]
    ) -> tuple[list[_Script], int]:
        gap = account.gap_limit
        initial_gap = gap
        scripts: list[_Script] = []
        new_scripts: list[_Script] = []
        for chain in (0, 1):
            chain_scripts = self._derive(account, chain, 0, gap)
            scripts.extend(chain_scripts)
            new_scripts.extend(chain_scripts)
        while True:
            pending = {s.key: s.script_hex for s in new_scripts}
            if not pending:
                break
            fetched = get_histories(self._client, pending)
            histories.update(fetched)
            extended: list[_Script] = []
            for chain in (0, 1):
                chain_items = [s for s in scripts if s.chain == chain]
                last_active = -1
                for item in chain_items:
                    if fetched.get(item.key):
                        last_active = max(last_active, item.index)
                current_gap = len(chain_items)
                if last_active >= current_gap - account.gap_limit and current_gap < MAX_GAP:
                    extra = min(account.gap_limit, MAX_GAP - current_gap)
                    extra_scripts = self._derive(account, chain, current_gap, extra)
                    scripts.extend(extra_scripts)
                    extended.extend(extra_scripts)
                elif current_gap >= MAX_GAP and last_active >= current_gap - account.gap_limit:
                    raise RuntimeError(
                        f"{account.id}: gap limit exceeded {MAX_GAP}; check descriptor/index"
                    )
            new_scripts = extended
            if not new_scripts:
                break
        extended_gap = max(
            len([s for s in scripts if s.chain == 0]),
            len([s for s in scripts if s.chain == 1]),
        )
        account.gap_limit = extended_gap
        return scripts, max(0, extended_gap - initial_gap)

    # -- sync ------------------------------------------------------------

    def sync_accounts(self, accounts: list[Account]) -> ElectrumSyncSummary:
        wallets = [account for account in accounts if account.kind == "wallet"]
        if not wallets:
            raise ValueError("electrum sync needs at least one wallet account")
        summary = ElectrumSyncSummary(accounts=len(wallets))
        histories: dict[str, list[HistoryEntry]] = {}
        all_scripts: dict[str, _Script] = {}
        initial_gaps = {account.id: account.gap_limit for account in wallets}
        for account in wallets:
            account_scripts, _extended = self._discover_scripts(account, histories)
            for script in account_scripts:
                all_scripts[script.key] = script
        summary.scripts = len(all_scripts)
        summary.gap_extended = sum(
            max(0, account.gap_limit - initial_gaps[account.id]) for account in wallets
        )
        self._scripts = all_scripts
        self._script_owner = {
            script.script_hex: script.account_id for script in all_scripts.values()
        }
        self._address_owner = {script.address: script.account_id for script in all_scripts.values()}
        txids = {entry.txid for entries in histories.values() for entry in entries}
        summary.txs_seen = len(txids)
        summary.last_height = max(
            (entry.height for entries in histories.values() for entry in entries), default=0
        )
        if not txids:
            self._stamp_sync(summary)
            return summary
        transactions = fetch_transactions(self._client, histories, batch_size=DEFAULT_BATCH_SIZE)
        for txid in sorted(txids):
            fetched = transactions.get(txid)
            if fetched is None:
                raise RuntimeError(f"Electrum did not return transaction {txid}")
            inserted = self._store_transaction(fetched)
            summary.new_txs += int(inserted)
        summary.outputs_attributed = self._attribute_outputs()
        self._stamp_sync(summary)
        return summary

    def _owner_for(self, address: object, script_hex: object) -> str | None:
        return self._script_owner.get(str(script_hex or "")) or self._address_owner.get(
            str(address or "")
        )

    def _store_transaction(self, fetched: RawTransaction) -> bool:
        raw = cast(dict[str, Any], fetched.raw)
        txid = str(raw["txid"])
        exists = self._db.execute("SELECT 1 FROM transactions WHERE txid=?", (txid,)).fetchone()
        block_height = fetched.height if fetched.height > 0 else None
        block_time = _iso(fetched.block_time)
        self._db.execute(
            "INSERT OR IGNORE INTO transactions "
            "(txid, block_height, block_time, is_coinbase) VALUES (?,?,?,?)",
            (txid, block_height, block_time, int(any("coinbase" in vin for vin in raw["vin"]))),
        )
        for index, vin in enumerate(cast(list[dict[str, Any]], raw.get("vin", []))):
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
        for vout in cast(list[dict[str, Any]], raw.get("vout", [])):
            script_pub_key = vout.get("scriptPubKey", {})
            address = script_pub_key.get("address")
            script_hex = script_pub_key.get("hex")
            owner = self._owner_for(address, script_hex)
            self._db.execute(
                "INSERT OR IGNORE INTO tx_outputs "
                "(txid, vout, address, script_hex, value_sat, is_mine, account_id) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    txid,
                    int(vout["n"]),
                    address,
                    script_hex,
                    _btc_to_sat(vout["value"]),
                    int(owner is not None),
                    owner,
                ),
            )
        self._db.commit()
        return exists is None

    def _attribute_outputs(self) -> int:
        count = 0
        rows = self._db.execute(
            "SELECT txid, vout, address, script_hex, account_id, is_mine FROM tx_outputs"
        ).fetchall()
        for txid, vout, address, script_hex, old_account, old_mine in rows:
            owner = self._owner_for(address, script_hex)
            if owner is not None:
                count += 1
                if owner != old_account or not old_mine:
                    self._db.execute(
                        "UPDATE tx_outputs SET account_id=?, is_mine=1 WHERE txid=? AND vout=?",
                        (owner, txid, vout),
                    )
            elif old_mine:
                self._db.execute(
                    "UPDATE tx_outputs SET account_id=NULL, is_mine=0 WHERE txid=? AND vout=?",
                    (txid, vout),
                )
        self._db.commit()
        return count

    def _stamp_sync(self, summary: ElectrumSyncSummary) -> None:
        now = datetime.datetime.now(datetime.UTC).isoformat()
        for key, value in (
            ("last_sync_at", now),
            ("last_height", str(summary.last_height)),
            ("last_backend", "electrum"),
        ):
            self._db.execute(
                "INSERT OR REPLACE INTO sync_state (key,value,updated_at) VALUES (?,?,?)",
                (key, value, now),
            )
        self._db.commit()
