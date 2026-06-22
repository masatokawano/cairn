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
