"""Sprint 0: full Sec. 8 schema is created now (no later retrofit)."""

import sqlite3

from utxoproof.db import open_memory_db

EXPECTED_TABLES = {
    "transactions",
    "tx_outputs",
    "tx_inputs",
    "price_cache",
    "sync_state",
    "source_manifest",
    "evidence_links",
    "provenance_links",
    "advisory_cache",
}


def test_all_tables_created() -> None:
    db = open_memory_db()
    tables = {
        row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    assert EXPECTED_TABLES <= tables


def test_kyc_defaults() -> None:
    db = open_memory_db()
    db.execute("INSERT INTO tx_outputs (txid, vout, value_sat) VALUES ('abc', 0, 100000)")
    row = db.execute("SELECT kyc_status, kyc_fraction FROM tx_outputs WHERE txid='abc'").fetchone()
    assert row == ("unknown", "0")


def test_source_manifest_dedupes_on_sha256() -> None:
    db = open_memory_db()
    db.execute(
        "INSERT INTO source_manifest "
        "(filename, sha256, size_bytes, role, imported_at) "
        "VALUES ('kraken_2023.csv', 'deadbeef', 10, 'source', '2024-01-01')"
    )
    try:
        db.execute(
            "INSERT INTO source_manifest "
            "(filename, sha256, size_bytes, role, imported_at) "
            "VALUES ('kraken_2023_copy.csv', 'deadbeef', 10, 'source', '2024-01-02')"
        )
    except sqlite3.IntegrityError:
        return
    raise AssertionError("duplicate sha256 import was not rejected")


def test_provenance_links_default_origin_and_unique_edge() -> None:
    db = open_memory_db()
    row = (
        "D",
        0,
        "chain",
        "C:0",
        "D:0",
        "exact",
        "tx C:0",
        None,
        150000000,
        "2026-10-28T00:00:00+00:00",
    )
    db.execute(
        "INSERT INTO provenance_links "
        "(utxo_txid, utxo_vout, layer, from_ref, to_ref, confidence, "
        "evidence_ref, amount_eur, amount_sat, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        row,
    )
    stored = db.execute("SELECT origin FROM provenance_links").fetchone()
    assert stored == ("derived",)
    try:
        db.execute(
            "INSERT INTO provenance_links "
            "(utxo_txid, utxo_vout, layer, from_ref, to_ref, confidence, "
            "evidence_ref, amount_eur, amount_sat, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            row,
        )
    except sqlite3.IntegrityError:
        return
    raise AssertionError("duplicate provenance edge was not rejected")


def test_is_mine_defaults_to_ours() -> None:
    db = open_memory_db()
    db.execute("INSERT INTO tx_outputs (txid, vout, value_sat) VALUES ('mine', 0, 100000)")
    row = db.execute("SELECT is_mine FROM tx_outputs WHERE txid='mine'").fetchone()
    assert row == (1,)


def test_migration_adds_is_mine_to_old_db() -> None:
    from utxoproof.db import init_db

    db = sqlite3.connect(":memory:")
    db.execute(
        "CREATE TABLE tx_outputs (txid TEXT NOT NULL, vout INTEGER NOT NULL, "
        "value_sat INTEGER NOT NULL, PRIMARY KEY (txid, vout))"
    )
    db.execute("INSERT INTO tx_outputs (txid, vout, value_sat) VALUES ('old', 0, 50000)")
    init_db(db)  # must add the column without touching existing rows
    row = db.execute("SELECT value_sat, is_mine FROM tx_outputs WHERE txid='old'").fetchone()
    assert row == (50000, 1)
    tables = {
        row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    assert "provenance_links" in tables
