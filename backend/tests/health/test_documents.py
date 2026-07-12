"""H4 medical document registry: immutable snapshot, provenance metadata,
extraction lifecycle (none/draft/verified), broken-reference detection.

Maps to ACCEPTANCE.md H4. Fixtures are synthetic (not real documents).
"""
from __future__ import annotations

from datetime import date

import pytest

from app.health import analytics, config, store
from app.health.importers import documents

from .conftest import FIXTURES

PDF = FIXTURES / "synthetic_document.pdf"
TEXT = FIXTURES / "synthetic_extracted.txt"


def _rows(home, sql, params=None):
    conn = store.connect(home.resolve())
    try:
        return conn.execute(sql, params or []).fetchall()
    finally:
        conn.close()


def test_register_snapshots_and_records_metadata(health_home):
    out = documents.register(PDF, kind="lab_report", title="Synthetic report",
                             document_date="2031-05-01", issuer="Synthetic Clinic")
    assert out["status"] == "registered"
    assert out["extraction_status"] == "none"

    home = health_home.resolve()
    # Immutable snapshot in raw/documents, mode 0600.
    snaps = list((home / "raw" / "documents").iterdir())
    assert len(snaps) == 1
    import stat
    assert stat.S_IMODE(snaps[0].stat().st_mode) == 0o600

    (row,) = _rows(health_home,
        "SELECT document_kind, title, document_date, issuer, extraction_status"
        " FROM documents")
    assert row == ("lab_report", "Synthetic report", date(2031, 5, 1),
                   "Synthetic Clinic", "none")
    (sf,) = _rows(health_home,
        "SELECT source_kind, sha256, size_bytes FROM source_files")
    assert sf[0] == "document"
    assert sf[1] and sf[2] > 0


def test_register_is_idempotent(health_home):
    first = documents.register(PDF, kind="lab_report")
    second = documents.register(PDF, kind="lab_report")
    assert second["status"] == "already_registered"
    assert second["document_id"] == first["document_id"]
    assert _rows(health_home, "SELECT count(*) FROM documents")[0][0] == 1
    assert _rows(health_home, "SELECT count(*) FROM source_files")[0][0] == 1


def test_unknown_kind_and_bad_date_rejected(health_home):
    with pytest.raises(documents.DocumentError, match="unknown kind"):
        documents.register(PDF, kind="tarot_reading")
    with pytest.raises(documents.DocumentError, match="YYYY-MM-DD"):
        documents.register(PDF, kind="imaging", document_date="May 2031")


def test_attach_text_defaults_to_draft_not_verified(health_home):
    doc = documents.register(PDF, kind="lab_report")
    out = documents.attach_text(doc["document_id"], TEXT)
    assert out["extraction_status"] == "draft"      # never silently verified
    (row,) = _rows(health_home,
        "SELECT extraction_status, extracted_text_path FROM documents")
    assert row[0] == "draft"
    assert row[1].startswith("derived/extracted/")
    # Text lives in the protected home, mode 0600.
    import stat
    p = health_home.resolve() / row[1]
    assert p.exists() and stat.S_IMODE(p.stat().st_mode) == 0o600


def test_attach_text_verified_is_explicit(health_home):
    doc = documents.register(PDF, kind="lab_report")
    out = documents.attach_text(doc["document_id"], TEXT, verified=True)
    assert out["extraction_status"] == "verified"


def test_attach_text_unknown_document_rejected(health_home):
    config.ensure_home()
    store.connect(create=True).close()
    with pytest.raises(documents.DocumentError, match="unknown document"):
        documents.attach_text("no-such-id", TEXT)


def test_broken_reference_detected_when_snapshot_removed(health_home):
    documents.register(PDF, kind="lab_report")
    home = health_home.resolve()
    # Simulate a lost snapshot (e.g. partial restore).
    (stored,) = _rows(health_home, "SELECT stored_path FROM source_files")
    (home / stored[0]).unlink()

    conn = store.connect(home)
    try:
        broken = analytics.broken_references(conn, home)
    finally:
        conn.close()
    assert len(broken) == 1
    assert broken[0]["kind"] == "missing_source_snapshot"


def test_broken_reference_detects_missing_extracted_text(health_home):
    doc = documents.register(PDF, kind="lab_report")
    documents.attach_text(doc["document_id"], TEXT)
    home = health_home.resolve()
    (path,) = _rows(health_home,
        "SELECT extracted_text_path FROM documents")
    (home / path[0]).unlink()

    conn = store.connect(home)
    try:
        broken = analytics.broken_references(conn, home)
    finally:
        conn.close()
    assert [b["kind"] for b in broken] == ["missing_extracted_text"]


def test_intact_store_has_no_broken_references(health_home):
    documents.register(PDF, kind="lab_report")
    home = health_home.resolve()
    conn = store.connect(home)
    try:
        assert analytics.broken_references(conn, home) == []
    finally:
        conn.close()


def test_logs_carry_no_document_content(health_home, caplog):
    import logging

    with caplog.at_level(logging.INFO, logger="cairn.health"):
        doc = documents.register(PDF, kind="lab_report", title="SECRET-TITLE",
                                 issuer="SECRET-CLINIC")
        documents.attach_text(doc["document_id"], TEXT)
    assert "SECRET-TITLE" not in caplog.text
    assert "SECRET-CLINIC" not in caplog.text
