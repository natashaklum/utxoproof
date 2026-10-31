"""BIP329 import semantics: account scope, conflicts and user precedence."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from utxoproof.db import open_memory_db
from utxoproof.labels import (
    LabelError,
    address_labels,
    import_bip329,
    label_text,
    output_labels,
    parse_bip329,
    set_user_label,
)

TXID = "a" * 64


def _write(path: Path, items: list[dict[str, object]]) -> Path:
    path.write_text("\n".join(json.dumps(item) for item in items) + "\n")
    return path


def test_parse_bip329_and_unknown_types(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "labels.jsonl",
        [
            {"type": "tx", "ref": TXID, "label": "Buy"},
            {"type": "output", "ref": f"{TXID}:0", "label": "Cold storage"},
            {"type": "addr", "ref": "bc1qexample", "label": "Savings"},
            {"type": "future", "ref": "x", "label": "Unknown kind"},
            {"type": "input", "ref": f"{TXID}:1", "label": "Spent"},
        ],
    )
    labels, warnings = parse_bip329(path)
    assert len(labels) == 5
    assert any("unknown BIP329 type" in warning for warning in warnings)
    assert labels[1].ref == f"{TXID}:0"


def test_invalid_ref_is_rejected(tmp_path: Path) -> None:
    path = _write(tmp_path / "labels.jsonl", [{"type": "output", "ref": "nope", "label": "x"}])
    with pytest.raises(LabelError, match="txid:vout"):
        parse_bip329(path)


def test_import_update_and_user_precedence(tmp_path: Path) -> None:
    db = open_memory_db()
    first = _write(
        tmp_path / "labels.jsonl",
        [
            {"type": "tx", "ref": TXID, "label": "Imported"},
            {"type": "output", "ref": f"{TXID}:0", "label": "UTXO"},
            {"type": "addr", "ref": "bc1qexample", "label": "Address"},
        ],
    )
    result = import_bip329(db, "wallet-a", first)
    assert (result.imported, result.updated, result.unchanged) == (3, 0, 0)
    assert label_text(db, "tx", TXID) == "Imported"
    assert output_labels(db)[f"{TXID}:0"] == "UTXO · Imported"
    assert address_labels(db)["bc1qexample"] == "Address"

    second = _write(
        tmp_path / "labels2.jsonl",
        [
            {"type": "tx", "ref": TXID, "label": "Imported v2"},
            {"type": "output", "ref": f"{TXID}:0", "label": "UTXO"},
        ],
    )
    result = import_bip329(db, "wallet-a", second)
    assert result.updated == 1
    assert result.unchanged == 1
    assert result.deleted == 1
    assert label_text(db, "tx", TXID) == "Imported v2"
    assert "bc1qexample" not in address_labels(db)

    set_user_label(db, "wallet-a", "tx", TXID, "My buy")
    conflict = _write(
        tmp_path / "labels3.jsonl", [{"type": "tx", "ref": TXID, "label": "Server says"}]
    )
    result = import_bip329(db, "wallet-a", conflict)
    assert result.conflicts and "user label" in result.conflicts[0]
    assert label_text(db, "tx", TXID) == "My buy"


def test_same_ref_in_two_accounts_is_scoped(tmp_path: Path) -> None:
    db = open_memory_db()
    path = _write(tmp_path / "labels.jsonl", [{"type": "tx", "ref": TXID, "label": "Shared"}])
    import_bip329(db, "wallet-a", path)
    import_bip329(db, "wallet-b", path)
    assert (
        len(db.execute("SELECT * FROM labels WHERE type='tx' AND ref=?", (TXID,)).fetchall()) == 2
    )
    assert label_text(db, "tx", TXID) == "Shared"


def test_label_cli_sets_and_deletes_user_label(tmp_path: Path) -> None:
    from utxoproof.cli import main
    from utxoproof.db import init_db

    db_path = tmp_path / "utxoproof.db"
    db = sqlite3.connect(db_path)
    init_db(db)
    db.execute("INSERT INTO accounts (id,kind,label) VALUES ('ledger-main','wallet','Ledger main')")
    db.commit()
    db.close()
    ref = "ab" * 32 + ":0"
    assert (
        main(
            [
                "label",
                "--db",
                str(db_path),
                "--account",
                "ledger-main",
                "--type",
                "output",
                "--ref",
                ref,
                "--label",
                "gift",
            ]
        )
        == 0
    )
    db = sqlite3.connect(db_path)
    assert db.execute("SELECT label, origin FROM labels").fetchall() == [("gift", "user")]
    db.close()
    assert (
        main(
            [
                "label",
                "--db",
                str(db_path),
                "--account",
                "ledger-main",
                "--type",
                "output",
                "--ref",
                ref,
                "--delete",
            ]
        )
        == 0
    )
    db = sqlite3.connect(db_path)
    assert db.execute("SELECT COUNT(*) FROM labels").fetchone()[0] == 0
    db.close()


def test_reimport_keeps_user_labels_when_the_export_drops_the_ref(tmp_path: Path) -> None:
    db = open_memory_db()
    path = _write(
        tmp_path / "labels.jsonl",
        [{"type": "tx", "ref": TXID, "label": "Imported"}],
    )
    import_bip329(db, "wallet-a", path)
    set_user_label(db, "wallet-a", "tx", TXID, "Mine")
    empty = _write(tmp_path / "empty.jsonl", [])
    result = import_bip329(db, "wallet-a", empty)
    assert result.deleted == 0
    assert label_text(db, "tx", TXID) == "Mine"
