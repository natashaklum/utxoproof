"""SQLite graph helpers."""

from __future__ import annotations

from utxoproof.db import open_memory_db, relink_spends


def test_relink_spends_recomputes_and_clears_spend_links() -> None:
    db = open_memory_db()
    # Inputs may arrive before the output they spend (same-block history order).
    db.execute(
        "INSERT INTO tx_inputs (txid,input_index,prev_txid,prev_vout) VALUES (?,?,?,?)",
        ("child", 0, "parent", 0),
    )
    db.execute(
        "INSERT INTO tx_outputs (txid,vout,value_sat) VALUES (?,?,?)",
        ("parent", 0, 1000),
    )
    db.commit()

    relink_spends(db)
    assert db.execute(
        "SELECT spent_by_txid, spent_by_input FROM tx_outputs WHERE txid='parent'"
    ).fetchone() == ("child", 0)

    db.execute("DELETE FROM tx_inputs")
    db.commit()
    relink_spends(db)
    assert db.execute(
        "SELECT spent_by_txid, spent_by_input FROM tx_outputs WHERE txid='parent'"
    ).fetchone() == (None, None)
    db.close()
