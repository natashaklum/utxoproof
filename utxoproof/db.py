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
def _migrate(db: sqlite3.Connection) -> None:
    cols = {row[1] for row in db.execute("PRAGMA table_info(tx_outputs)").fetchall()}
    if "is_mine" not in cols:
        db.execute("ALTER TABLE tx_outputs ADD COLUMN is_mine INTEGER NOT NULL DEFAULT 1")
    db.commit()


def open_memory_db() -> sqlite3.Connection:
    """In-memory DB with schema applied (used by tests)."""
    db = sqlite3.connect(":memory:")
    init_db(db)
    return db
