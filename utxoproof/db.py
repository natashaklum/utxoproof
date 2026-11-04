"""SQLite helpers for Sprint 0: create the full Sec. 8 schema now."""

from __future__ import annotations

import sqlite3
from importlib import resources


def init_db(db: sqlite3.Connection) -> None:
    """Create all utxoproof tables if absent, migrating old DBs forward."""
    schema = resources.files("utxoproof").joinpath("schema.sql").read_text(encoding="utf-8")
    db.executescript(schema)
    _migrate(db)


# Migration notes (idempotent; each guarded by PRAGMA table_info):
# - 2026-10-13: tx_outputs.is_mine (ownership-aware sync; old rows keep 1 =
#   assumed ours, corrected by the sync ownership pass or `reconcile`).
# - 2026-10-28: provenance_links (Sprint 12 saga edges). The schema script
#   creates the table idempotently on old DBs; no column migration is needed.
# - 2026-10-30: plan v8 accounts/account_scripts/labels + tx_outputs.account_id
#   and script_hex (per-output ownership). Tables come from the schema script;
#   only columns need ALTER TABLE on old DBs.
def _migrate(db: sqlite3.Connection) -> None:
    cols = {row[1] for row in db.execute("PRAGMA table_info(tx_outputs)").fetchall()}
    if "is_mine" not in cols:
        db.execute("ALTER TABLE tx_outputs ADD COLUMN is_mine INTEGER NOT NULL DEFAULT 1")
    if "account_id" not in cols:
        db.execute("ALTER TABLE tx_outputs ADD COLUMN account_id TEXT")
    if "script_hex" not in cols:
        db.execute("ALTER TABLE tx_outputs ADD COLUMN script_hex TEXT")
    # Indexes created here (not in schema.sql) so old DBs gain columns first.
    db.execute("CREATE INDEX IF NOT EXISTS idx_tx_outputs_account ON tx_outputs(account_id)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_tx_outputs_script ON tx_outputs(script_hex)")
    db.commit()


def open_memory_db() -> sqlite3.Connection:
    """In-memory DB with schema applied (used by tests)."""
    db = sqlite3.connect(":memory:")
    init_db(db)
    return db


def relink_spends(db: sqlite3.Connection) -> None:
    """Recompute ``tx_outputs.spent_by_*`` from the full input graph.

    Sync backends can see a spending transaction before the transaction that
    created the output (same-block ordering, multi-account overlap). A single
    post-pass makes the spend relation independent of arrival order and clears
    stale links after a reorg.
    """
    db.execute(
        "UPDATE tx_outputs SET "
        "spent_by_txid=(SELECT i.txid FROM tx_inputs i "
        " WHERE i.prev_txid=tx_outputs.txid AND i.prev_vout=tx_outputs.vout LIMIT 1), "
        "spent_by_input=(SELECT i.input_index FROM tx_inputs i "
        " WHERE i.prev_txid=tx_outputs.txid AND i.prev_vout=tx_outputs.vout LIMIT 1) "
        "WHERE EXISTS (SELECT 1 FROM tx_inputs i "
        " WHERE i.prev_txid=tx_outputs.txid AND i.prev_vout=tx_outputs.vout)"
    )
    db.execute(
        "UPDATE tx_outputs SET spent_by_txid=NULL, spent_by_input=NULL "
        "WHERE spent_by_txid IS NOT NULL AND NOT EXISTS ("
        "SELECT 1 FROM tx_inputs i WHERE i.prev_txid=tx_outputs.txid "
        "AND i.prev_vout=tx_outputs.vout)"
    )
    db.commit()
