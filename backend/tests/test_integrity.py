"""Tests for the read-only integrity-check audit (P1-D)."""
import importlib

import pytest


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("CAIRN_DB", str(tmp_path / "test.db"))
    from app import db as db_module
    importlib.reload(db_module)
    yield db_module
    conn = getattr(db_module._local, "conn", None)
    if conn:
        conn.close()
        db_module._local.conn = None


def make_conv(db, source_id="c1"):
    from app.parsers.base import ParsedConversation, ParsedMessage
    return ParsedConversation(
        source="chatgpt", source_id=source_id, title="検査テスト",
        messages=[ParsedMessage(role="user", text="本文サンプル", created_at="2025-01-01T00:00:00Z")],
        created_at="2025-01-01T00:00:00Z", updated_at="2025-01-01T00:10:00Z",
    )


def test_clean_db_passes(db):
    db.upsert_conversations([make_conv(db), make_conv(db, "c2")])
    report = db.integrity_check()
    assert report["ok"] is True
    assert report["problems"] == []
    assert report["checks"]["sqlite_integrity_check"] == ["ok"]
    assert report["checks"]["messages"] == report["checks"]["messages_fts"]
    assert report["checks"]["fts_integrity"] == "ok"


def test_detects_orphan_message(db):
    db.upsert_conversations([make_conv(db)])
    conn = db.connect()
    conn.execute("PRAGMA foreign_keys = OFF")  # bypass FK to inject an orphan
    with conn:
        conn.execute(
            "INSERT INTO messages (conversation_id, idx, role, text) VALUES (9999, 0, 'user', 'orphan')"
        )
    conn.execute("PRAGMA foreign_keys = ON")
    report = db.integrity_check()
    assert report["ok"] is False
    assert report["checks"]["orphan_messages"] == 1
    assert any("orphan messages" in p for p in report["problems"])


def test_detects_fts_desync(db):
    db.upsert_conversations([make_conv(db)])
    conn = db.connect()
    row = conn.execute("SELECT id, text FROM messages LIMIT 1").fetchone()
    # Remove the row from the FTS index only (messages table keeps it).
    with conn:
        conn.execute(
            "INSERT INTO messages_fts(messages_fts, rowid, text) VALUES ('delete', ?, ?)",
            (row["id"], row["text"]),
        )
    report = db.integrity_check()
    assert report["ok"] is False
    assert report["checks"]["messages_fts"] < report["checks"]["messages"]
    assert any("FTS row count mismatch" in p for p in report["problems"])


def test_detects_blank_source(db):
    conn = db.connect()
    with conn:
        conn.execute(
            "INSERT INTO conversations (source, source_id, title, content_hash) VALUES ('', '', 't', 'h')"
        )
    report = db.integrity_check()
    assert report["ok"] is False
    assert report["checks"]["blank_source_or_source_id"] == 1


def test_clean_db_reports_items_checks(db):
    """M0: a healthy DB reports every conversation mirrored into items,
    every chunk carrying an item_id, and no drift."""
    db.upsert_conversations([make_conv(db), make_conv(db, "c2")])
    report = db.integrity_check()
    assert report["ok"] is True
    checks = report["checks"]
    assert checks["conversations_missing_item"] == 0
    assert checks["chunks_missing_item_id"] == 0
    assert checks["items_without_conversation"] == 0
    assert checks["items_conversation_drift"] == 0


def test_detects_conversation_without_item(db):
    """A conversation without its items row is a hard problem — cross-source
    RRF would silently drop it. FK is disabled here to inject the drift the
    integrity check is meant to spot."""
    db.upsert_conversations([make_conv(db)])
    conn = db.connect()
    conn.execute("PRAGMA foreign_keys = OFF")
    with conn:
        conn.execute("UPDATE chunks SET item_id = NULL")  # break the FK dep first
        conn.execute("DELETE FROM items WHERE source='chatgpt' AND external_id='c1'")
    conn.execute("PRAGMA foreign_keys = ON")
    report = db.integrity_check()
    assert report["ok"] is False
    assert report["checks"]["conversations_missing_item"] == 1
    assert any("without a matching items row" in p for p in report["problems"])


def test_detects_chunks_missing_item_id(db):
    """A NULL chunks.item_id after backfill is a hard problem."""
    db.upsert_conversations([make_conv(db)])
    conn = db.connect()
    with conn:
        conn.execute("UPDATE chunks SET item_id = NULL")
    report = db.integrity_check()
    assert report["ok"] is False
    assert report["checks"]["chunks_missing_item_id"] >= 1
    assert any("NULL item_id" in p for p in report["problems"])


def test_reports_items_conversation_drift_as_info(db):
    """redact-apply-style direct edits to conversations show up as drift.
    Info only (not appended to problems): admin.py stays frozen through M5
    per DESIGN.md §5.7."""
    db.upsert_conversations([make_conv(db)])
    conn = db.connect()
    with conn:
        conn.execute("UPDATE conversations SET title='別のタイトル' WHERE id=1")
    report = db.integrity_check()
    assert report["ok"] is True  # drift is info-only
    assert report["checks"]["items_conversation_drift"] == 1


def test_admin_command_exit_codes(db, capsys):
    import json
    from app import admin
    importlib.reload(admin)
    db.upsert_conversations([make_conv(db)])
    assert admin.main(["integrity-check"]) == 0  # clean → 0
    assert json.loads(capsys.readouterr().out)["ok"] is True

    conn = db.connect()
    conn.execute("PRAGMA foreign_keys = OFF")
    with conn:
        conn.execute(
            "INSERT INTO messages (conversation_id, idx, role, text) VALUES (9999, 0, 'user', 'x')"
        )
    assert admin.main(["integrity-check"]) == 2  # problems → 2
    assert json.loads(capsys.readouterr().out)["ok"] is False
