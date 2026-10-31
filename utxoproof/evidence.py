"""Evidence packaging (plan Sec. 15, Sprint 9).

Assembles the reproducible evidence bundle for a tax year: raw sources,
intermediate inputs, the HTML report, a manifest SQLite DB copy, and a
SHA-256 manifest. Duplicate source imports are rejected on content hash.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import shutil
import sqlite3
import zipfile
from pathlib import Path

from utxoproof import __version__
from utxoproof.db import init_db


def sha256_file(path: str | Path) -> str:
    """SHA-256 hex digest of a file."""
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def record_source(
    db: sqlite3.Connection, path: str | Path, role: str, name: str | None = None
) -> bool:
    """Record a source file in the manifest. False when its hash exists.

    ``name`` overrides the stored filename (used for evidence files, stored
    relative to the evidence root, e.g. ``2023/receipt.png``).
    """
    file = Path(path)
    digest = sha256_file(file)
    try:
        db.execute(
            "INSERT INTO source_manifest (filename, sha256, size_bytes, role, imported_at)"
            " VALUES (?,?,?,?,?)",
            (
                name or file.name,
                digest,
                file.stat().st_size,
                role,
                datetime.datetime.now(datetime.UTC).isoformat(),
            ),
        )
        db.commit()
    except sqlite3.IntegrityError:
        return False  # duplicate content already recorded
    return True


IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
# Thumbnails above this are replaced by a file link (keeps pages light).
THUMBNAIL_MAX_BYTES = 2_000_000


def attach_file(
    db: sqlite3.Connection,
    src: str | Path,
    evidence_root: str | Path,
    year: int,
    txid: str | None = None,
    note: str = "",
) -> dict[str, str]:
    """Copy ``src`` into ``evidence_root/<year>/``, hash, record and link it.

    Name collisions gain a numeric suffix. Returns the manifest entry
    (filename relative to root, sha256, txid, note).
    """
    src_path = Path(src)
    if not src_path.is_file():
        raise ValueError(f"attachment not found: {src}")
    dest_dir = Path(evidence_root) / str(year)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src_path.name
    counter = 1
    while dest.exists():
        dest = dest_dir / f"{src_path.stem}-{counter}{src_path.suffix}"
        counter += 1
    shutil.copy2(src_path, dest)
    digest = sha256_file(dest)
    rel = f"{year}/{dest.name}"
    record_source(db, dest, "attachment", name=rel)
    db.execute(
        "INSERT INTO evidence_links (sha256, txid, note, created_at) VALUES (?,?,?,?)",
        (digest, txid, note, datetime.datetime.now(datetime.UTC).isoformat()),
    )
    db.commit()
    return {"filename": rel, "sha256": digest, "txid": txid or "", "note": note}


def attachments_for_txids(
    db: sqlite3.Connection, evidence_root: str | Path, txids: list[str]
) -> list[dict[str, str]]:
    """Manifest + link rows for attachments bound to any of ``txids``."""
    wanted = set(txids)
    if not wanted:
        return []
    rows = [
        row
        for row in db.execute(
            "SELECT m.filename, m.sha256, l.txid, l.note, m.size_bytes "
            "FROM evidence_links l "
            "JOIN source_manifest m ON m.sha256=l.sha256 "
            "ORDER BY m.filename"
        ).fetchall()
        if row[2] in wanted
    ]
    return [
        {
            "filename": row[0],
            "sha256": row[1],
            "txid": row[2] or "",
            "note": row[3] or "",
            "size_bytes": str(row[4]),
            "path": str(Path(evidence_root) / row[0]),
        }
        for row in rows
    ]


def thumbnail_data_uri(path: str | Path) -> str | None:
    """Base64 data URI for small images; None for other types/large files."""
    file = Path(path)
    if file.suffix.lower() not in IMAGE_EXTENSIONS:
        return None
    try:
        if file.stat().st_size > THUMBNAIL_MAX_BYTES:
            return None
        import base64

        mime = "jpeg" if file.suffix.lower() in (".jpg", ".jpeg") else file.suffix[1:].lower()
        raw = file.read_bytes()
        encoded = base64.b64encode(raw).decode("ascii")
        return f"data:image/{mime};base64,{encoded}"
    except OSError:
        return None


def produce_evidence_zip(
    year: int,
    manifest_db_path: str | Path,
    report_html_path: str | Path,
    sources: list[Path],
    intermediate: list[Path],
    output_dir: str | Path,
    timestamp: str | None = None,
    attachments: list[Path] | None = None,
    arcnames: dict[str, str] | None = None,
) -> Path:
    """Build ``utxoproof_evidence_<year>_<timestamp>.zip``. Returns its path."""
    stamp = timestamp or datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%SZ")
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    zip_path = out / f"utxoproof_evidence_{year}_{stamp}.zip"

    manifest_files: list[dict[str, object]] = []
    manifest: dict[str, object] = {
        "generated_at": stamp,
        "tax_year": year,
        "utxoproof_version": __version__,
        "files": manifest_files,
    }

    used_arcnames: set[str] = {"report.html", "evidence.db", "manifest.json"}

    def add_file(zf: zipfile.ZipFile, file: str | Path, arc_dir: str, role: str) -> None:
        name = Path(file).name
        custom = (arcnames or {}).get(str(Path(file).resolve()))
        arcname = f"{arc_dir}/{custom}" if custom else f"{arc_dir}/{name}"
        if arcname in used_arcnames:
            stem = Path(file).stem
            suffix = Path(file).suffix
            counter = 1
            while arcname in used_arcnames:
                arcname = f"{arc_dir}/{stem}-{counter}{suffix}"
                counter += 1
        used_arcnames.add(arcname)
        zf.write(file, arcname=arcname)
        manifest_files.append({"path": arcname, "sha256": sha256_file(file), "role": role})

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for role, arc_dir, paths in (
            ("source", "sources", sources),
            ("intermediate", "intermediate", intermediate),
            ("attachment", "attachments", attachments or []),
        ):
            for file in paths:
                add_file(zf, file, arc_dir, role)
        zf.write(report_html_path, arcname="report.html")
        manifest_files.append(
            {
                "path": "report.html",
                "sha256": sha256_file(report_html_path),
                "role": "report",
            }
        )
        zf.write(manifest_db_path, arcname="evidence.db")
        zf.writestr("manifest.json", json.dumps(manifest, indent=2))
    return zip_path


def build_manifest_db(path: str | Path) -> sqlite3.Connection:
    """Create a manifest-only evidence DB at ``path``."""
    db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(db_path))
    init_db(db)
    return db
