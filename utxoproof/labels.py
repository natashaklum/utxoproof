"""BIP329 labels and first-class user labels (plan v8).

BIP329 is a JSON-lines label exchange format, not a wallet/descriptor format.
Imported labels are assertions scoped to an account; descriptors remain the
only source of output ownership. User labels are never overwritten.
"""

from __future__ import annotations

import datetime
import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

LABEL_TYPES = ("tx", "addr", "output", "input")
KNOWN_TYPES = frozenset(LABEL_TYPES)


class LabelError(ValueError):
    """Invalid BIP329 data."""


@dataclass
class BIP329Label:
    type: str
    ref: str
    label: str
    spendable: bool | None = None
    line: int = 0


@dataclass
class ImportResult:
    account_id: str
    path: str
    imported: int = 0
    updated: int = 0
    unchanged: int = 0
    deleted: int = 0
    conflicts: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _validate_ref(kind: str, ref: str) -> str:
    ref = ref.strip()
    if not ref:
        raise LabelError("label ref is empty")
    if kind in ("output", "input"):
        txid, sep, vout = ref.rpartition(":")
        if not sep or len(txid) != 64 or not vout.isdigit():
            raise LabelError(f"{kind} ref must be txid:vout, got {ref!r}")
    elif kind == "tx":
        if len(ref) != 64:
            raise LabelError(f"tx ref must be a 64-char txid, got {ref!r}")
    elif kind == "addr":
        if any(ch.isspace() for ch in ref):
            raise LabelError(f"addr ref contains whitespace: {ref!r}")
    return ref


def parse_bip329(path: str | Path) -> tuple[list[BIP329Label], list[str]]:
    """Parse a BIP329 JSONL file strictly, preserving unknown types as warnings."""
    labels: list[BIP329Label] = []
    warnings: list[str] = []
    seen: set[tuple[str, str]] = set()
    source = Path(path)
    try:
        lines = source.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise LabelError(f"{source}: {exc}") from exc
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise LabelError(f"{source}:{number}: invalid JSON: {exc}") from exc
        if not isinstance(item, dict):
            raise LabelError(f"{source}:{number}: label must be a JSON object")
        kind = str(item.get("type", "")).strip()
        ref = str(item.get("ref", "")).strip()
        text = str(item.get("label", ""))
        if not kind or not ref:
            raise LabelError(f"{source}:{number}: label needs type and ref")
        try:
            ref = _validate_ref(kind, ref)
        except LabelError as exc:
            raise LabelError(f"{source}:{number}: {exc}") from exc
        if kind not in KNOWN_TYPES:
            warnings.append(f"{source}:{number}: unknown BIP329 type {kind!r} preserved")
        key = (kind, ref)
        if key in seen:
            warnings.append(f"{source}:{number}: duplicate {kind} label for {ref}; last wins")
        seen.add(key)
        spendable = item.get("spendable")
        labels.append(
            BIP329Label(
                type=kind,
                ref=ref,
                label=text,
                spendable=bool(spendable) if isinstance(spendable, bool) else None,
                line=number,
            )
        )
    return labels, warnings


def import_bip329(
    db: sqlite3.Connection, account_id: str, path: str | Path, origin: str = "bip329"
) -> ImportResult:
    """Upsert one account's BIP329 file. User rows always win."""
    source = Path(path)
    labels, warnings = parse_bip329(source)
    result = ImportResult(account_id=account_id, path=str(source), warnings=warnings)
    now = datetime.datetime.now(datetime.UTC).isoformat()
    present = {(item.type, item.ref) for item in labels}
    if origin != "user":
        # Re-import replaces the imported set: labels removed from the export
        # disappear, while user labels are never considered here.
        stale = db.execute(
            "SELECT type, ref FROM labels WHERE account_id=? AND origin=?",
            (account_id, origin),
        ).fetchall()
        for kind, ref in stale:
            if (str(kind), str(ref)) not in present:
                db.execute(
                    "DELETE FROM labels WHERE account_id=? AND type=? AND ref=? AND origin=?",
                    (account_id, kind, ref, origin),
                )
                result.deleted += 1
    for item in labels:
        row = db.execute(
            "SELECT label, spendable, origin, source_file, source_line FROM labels "
            "WHERE account_id=? AND type=? AND ref=?",
            (account_id, item.type, item.ref),
        ).fetchone()
        if row is None:
            db.execute(
                "INSERT INTO labels (account_id,type,ref,label,spendable,origin,"
                "source_file,source_line,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    account_id,
                    item.type,
                    item.ref,
                    item.label,
                    None if item.spendable is None else int(item.spendable),
                    origin,
                    str(source),
                    item.line,
                    now,
                ),
            )
            result.imported += 1
            continue
        old_label, old_spendable, old_origin, _old_file, _old_line = row
        if old_origin == "user" and origin != "user":
            if old_label != item.label:
                result.conflicts.append(
                    f"{source}:{item.line}: user label {old_label!r} keeps precedence "
                    f"over imported {item.label!r} for {item.type}:{item.ref}"
                )
            result.unchanged += 1
            continue
        spendable = None if item.spendable is None else int(item.spendable)
        if old_label == item.label and old_spendable == spendable:
            result.unchanged += 1
            continue
        db.execute(
            "UPDATE labels SET label=?, spendable=?, origin=?, source_file=?, "
            "source_line=?, updated_at=? WHERE account_id=? AND type=? AND ref=?",
            (
                item.label,
                spendable,
                origin,
                str(source),
                item.line,
                now,
                account_id,
                item.type,
                item.ref,
            ),
        )
        result.updated += 1
    db.commit()
    return result


def set_user_label(
    db: sqlite3.Connection, account_id: str, kind: str, ref: str, label: str
) -> None:
    """Create/replace a user label. Empty label deletes it."""
    ref = _validate_ref(kind, ref)
    if not label.strip():
        db.execute(
            "DELETE FROM labels WHERE account_id=? AND type=? AND ref=?",
            (account_id, kind, ref),
        )
        db.commit()
        return
    now = datetime.datetime.now(datetime.UTC).isoformat()
    db.execute(
        "INSERT INTO labels (account_id,type,ref,label,spendable,origin,source_file,"
        "source_line,updated_at) VALUES (?,?,?,?,NULL,'user','',NULL,?) "
        "ON CONFLICT(account_id,type,ref) DO UPDATE SET label=excluded.label,"
        "spendable=NULL,origin='user',updated_at=excluded.updated_at",
        (account_id, kind, ref, label.strip(), now),
    )
    db.commit()


def labels_for(
    db: sqlite3.Connection, kind: str, ref: str, account_id: str | None = None
) -> list[tuple[str, str, str]]:
    """Return ``(account_id, label, origin)`` for one ref in deterministic order."""
    if account_id is None:
        rows = db.execute(
            "SELECT account_id, label, origin FROM labels WHERE type=? AND ref=? "
            "ORDER BY account_id, origin DESC, id",
            (kind, ref),
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT account_id, label, origin FROM labels WHERE account_id=? AND type=? "
            "AND ref=? ORDER BY origin DESC, id",
            (account_id, kind, ref),
        ).fetchall()
    return [(str(a), str(b), str(c)) for a, b, c in rows]


def label_text(db: sqlite3.Connection, kind: str, ref: str, separator: str = " · ") -> str:
    """Display text for a ref across accounts; empty when unlabeled."""
    values: list[str] = []
    for _account, label, _origin in labels_for(db, kind, ref):
        if label and label not in values:
            values.append(label)
    return separator.join(values)


def output_labels(db: sqlite3.Connection) -> dict[str, str]:
    """Map ``txid:vout`` -> display labels, including transaction labels.

    Output labels win; transaction labels are prefixed with ``tx:`` only when
    no output label exists, which keeps tables compact and searchable.
    """
    out: dict[str, list[str]] = {}
    for kind, ref, label in db.execute(
        "SELECT type, ref, label FROM labels WHERE type IN ('output','tx') ORDER BY id"
    ).fetchall():
        if not label:
            continue
        if kind == "output":
            out.setdefault(str(ref), []).insert(0, str(label))
        else:
            out.setdefault(str(ref) + ":*", []).append(str(label))
    merged: dict[str, str] = {}
    for key, values in out.items():
        if key.endswith(":*"):
            continue
        merged[key] = " · ".join(dict.fromkeys(values + out.get(key.split(":")[0] + ":*", [])))
    for key, values in out.items():
        if key.endswith(":*"):
            txid = key[:-2]
            if not any(k.startswith(txid + ":") for k in merged):
                merged[txid + ":*"] = " · ".join(dict.fromkeys(values))
    return merged


def address_labels(db: sqlite3.Connection) -> dict[str, str]:
    out: dict[str, list[str]] = {}
    for _account, ref, label in db.execute(
        "SELECT account_id, ref, label FROM labels WHERE type='addr' ORDER BY id"
    ).fetchall():
        if not label:
            continue
        values = out.setdefault(str(ref), [])
        if str(label) not in values:
            values.append(str(label))
    return {ref: " · ".join(values) for ref, values in out.items()}
