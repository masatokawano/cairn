"""Medical document registry (H4, docs/health/DESIGN.md §4.5, DATA_MODEL §2.7).

Connects observations and interpretations to their documentary provenance
WITHOUT auto-extracting anything. A document (lab-report PDF, endoscopy
image, prescription scan …) is:

1. hashed and snapshotted immutably into ``raw/documents/`` (never modified);
2. registered in ``documents`` with clinical metadata and
   ``extraction_status='none'``.

Extracted text is a separate, explicit step (``attach_text``): OCR or manual
transcription output starts as ``draft`` and only becomes ``verified`` when a
human passes ``verified=True`` — extracted text is never silently trusted as
source fact (ACCEPTANCE H4). OCR itself is a later phase; this module only
establishes the provenance and verification lifecycle.

Idempotency: registration is keyed on the file content hash, so re-importing
the same document reuses its ``source_files`` row and refuses to create a
duplicate ``documents`` row for the same (source_file, kind).
"""
from __future__ import annotations

import hashlib
import logging
import os
import shutil
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

from .. import config, store

logger = logging.getLogger("cairn.health")

PARSER_NAME = "documents"
PARSER_VERSION = "1"

KINDS = frozenset({
    "lab_report", "imaging", "endoscopy", "prescription", "clinical_note",
    "referral", "discharge_summary", "vaccination", "other",
})
STATUSES = frozenset({"none", "draft", "verified"})


class DocumentError(Exception):
    """Invalid document registration or extraction request."""


def _sha256_file(src: Path) -> str:
    h = hashlib.sha256()
    with src.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _snapshot(home: Path, src: Path, sha256: str) -> Path:
    raw_dir = home / "raw" / PARSER_NAME
    raw_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(raw_dir, config.DIR_MODE)
    target = raw_dir / f"{sha256[:16]}_{src.name}"
    if not target.exists():
        shutil.copy2(src, target)
    config.protect_file(target)
    return target


def register(source: str | Path, *, kind: str, title: str | None = None,
             document_date: str | None = None, issuer: str | None = None,
             home: Path | None = None) -> dict:
    """Register a medical document (immutable snapshot + metadata row)."""
    if kind not in KINDS:
        raise DocumentError(f"unknown kind {kind!r}; allowed: {sorted(KINDS)}")
    doc_date: date | None = None
    if document_date:
        try:
            doc_date = datetime.strptime(document_date, "%Y-%m-%d").date()
        except ValueError:
            raise DocumentError(f"document_date must be YYYY-MM-DD: {document_date!r}")

    src = Path(source).expanduser()
    if not src.is_file():
        raise DocumentError(f"not a readable file: {src.name}")

    home = home or config.ensure_home()
    digest = _sha256_file(src)
    stored = _snapshot(home, src, digest)
    now = datetime.now(timezone.utc)

    conn = store.connect(home, create=True)
    try:
        row = conn.execute(
            "SELECT id FROM source_files WHERE sha256 = ?", [digest]
        ).fetchone()
        if row:
            source_file_id = row[0]
        else:
            source_file_id = uuid.uuid4().hex
            conn.execute(
                "INSERT INTO source_files (id, source_kind, original_name,"
                " stored_path, sha256, size_bytes, acquired_at, parser_name,"
                " parser_version, status) VALUES (?,?,?,?,?,?,?,?,?,?)",
                [source_file_id, "document", src.name,
                 str(stored.relative_to(home)), digest, src.stat().st_size,
                 now, PARSER_NAME, PARSER_VERSION, "imported"],
            )

        existing = conn.execute(
            "SELECT id FROM documents WHERE source_file_id=? AND document_kind=?",
            [source_file_id, kind],
        ).fetchone()
        if existing:
            logger.info("document already registered id=%s", existing[0])
            return {"document_id": existing[0], "source_sha256": digest[:16],
                    "status": "already_registered"}

        doc_id = uuid.uuid4().hex
        conn.execute(
            "INSERT INTO documents (id, document_kind, title, document_date,"
            " source_file_id, issuer, extraction_status, imported_at)"
            " VALUES (?,?,?,?,?,?, 'none', ?)",
            [doc_id, kind, title or src.name, doc_date, source_file_id,
             issuer, now],
        )
    finally:
        conn.close()

    logger.info("document registered id=%s kind=%s", doc_id, kind)
    return {"document_id": doc_id, "source_sha256": digest[:16],
            "extraction_status": "none", "status": "registered"}


def attach_text(document_id: str, text_file: str | Path, *,
                verified: bool = False, home: Path | None = None) -> dict:
    """Attach extracted/transcribed text to a document.

    Stored under ``derived/extracted/``. Status becomes ``verified`` only when
    ``verified=True`` is passed explicitly; otherwise ``draft`` (OCR output is
    never silently promoted to verified, ACCEPTANCE H4)."""
    home = home or config.resolve_home()
    text_path = Path(text_file).expanduser()
    if not text_path.is_file():
        raise DocumentError(f"not a readable text file: {text_path.name}")

    conn = store.connect(home)
    try:
        row = conn.execute(
            "SELECT id FROM documents WHERE id = ?", [document_id]
        ).fetchone()
        if not row:
            raise DocumentError(f"unknown document: {document_id!r}")

        dest_dir = home / "derived" / "extracted"
        dest_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(dest_dir, config.DIR_MODE)
        dest = dest_dir / f"{document_id}.txt"
        shutil.copyfile(text_path, dest)
        config.protect_file(dest)

        status = "verified" if verified else "draft"
        conn.execute(
            "UPDATE documents SET extracted_text_path=?, extraction_status=?"
            " WHERE id=?",
            [str(dest.relative_to(home)), status, document_id],
        )
    finally:
        conn.close()
    logger.info("document text attached id=%s status=%s", document_id, status)
    return {"document_id": document_id, "extraction_status": status}
