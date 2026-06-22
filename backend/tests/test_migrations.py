"""Tests for schema versioning and the migration runner (app.db).

Covers P1-A: a fresh DB is stamped to the current version without running
migrations, while an existing DB is brought up to date by _MIGRATIONS, with a
backup taken first and existing data preserved.
"""
import glob
import importlib
import os

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


def _reset_conn(db):
    conn = getattr(db._local, "conn", None)
    if conn:
        conn.close()
        db._local.conn = None


def _user_version(db):
    return db.connect().execute("PRAGMA user_version").fetchone()[0]


def make_conv(db, source_id="c1"):
    from app.parsers.base import ParsedConversation, ParsedMessage
    return ParsedConversation(
        source="chatgpt", source_id=source_id, title="テスト会話",
        messages=[ParsedMessage(role="user", text="本文", created_at="2025-01-01T00:00:00Z")],
        created_at="2025-01-01T00:00:00Z", updated_at="2025-01-01T00:10:00Z",
    )


def test_fresh_db_stamped_to_current_version(db):
    # A brand-new DB is built from _SCHEMA and stamped directly.
    db.connect()
    assert _user_version(db) == db._SCHEMA_VERSION


def test_fresh_db_does_not_run_migrations(db, monkeypatch, tmp_path):
    # Even with a pending migration defined, a fresh DB must not run it (the
    # latest _SCHEMA already represents that version) and must not back up.
    nxt = db._SCHEMA_VERSION + 1
    monkeypatch.setattr(db, "_MIGRATIONS",
                        [(nxt, "ALTER TABLE conversations ADD COLUMN never_runs INTEGER;")])
    monkeypatch.setattr(db, "_SCHEMA_VERSION", nxt)
    db.connect()
    assert _user_version(db) == nxt
    cols = [r[1] for r in db.connect().execute("PRAGMA table_info(conversations)")]
    assert "never_runs" not in cols
    assert glob.glob(str(tmp_path / "*.premigrate-*")) == []


def test_existing_db_migrated_with_backup(db, monkeypatch, tmp_path):
    # 1. Build an existing DB at the current version with real data.
    base = db._SCHEMA_VERSION
    db.upsert_conversations([make_conv(db)])
    assert _user_version(db) == base
    _reset_conn(db)

    # 2. Ship a new migration that adds a column; bump the target version.
    nxt = base + 1
    monkeypatch.setattr(
        db, "_MIGRATIONS",
        [(nxt, "ALTER TABLE conversations ADD COLUMN priority INTEGER NOT NULL DEFAULT 0;")],
    )
    monkeypatch.setattr(db, "_SCHEMA_VERSION", nxt)

    # 3. Reopening applies the pending migration.
    db.connect()

    # version bumped, new column present
    assert _user_version(db) == nxt
    cols = [r[1] for r in db.connect().execute("PRAGMA table_info(conversations)")]
    assert "priority" in cols

    # existing data preserved and still searchable
    full = db.get_conversation(1)
    assert full is not None and len(full["messages"]) == 1
    assert db.search("本文")[0]["conversation_id"] == 1

    # a pre-migration backup was created (and is locked down)
    backups = glob.glob(str(tmp_path / f"*.premigrate-v{base}-to-v{nxt}-*"))
    assert len(backups) == 1
    assert oct(os.stat(backups[0]).st_mode & 0o777) == "0o600"


def test_migrations_run_in_order_and_are_idempotent(db, monkeypatch):
    base = db._SCHEMA_VERSION
    db.upsert_conversations([make_conv(db)])
    _reset_conn(db)

    monkeypatch.setattr(db, "_MIGRATIONS", [
        (base + 1, "ALTER TABLE conversations ADD COLUMN a INTEGER DEFAULT 0;"),
        (base + 2, "ALTER TABLE conversations ADD COLUMN b INTEGER DEFAULT 0;"),
    ])
    monkeypatch.setattr(db, "_SCHEMA_VERSION", base + 2)

    db.connect()
    assert _user_version(db) == base + 2
    cols = [r[1] for r in db.connect().execute("PRAGMA table_info(conversations)")]
    assert {"a", "b"} <= set(cols)

    # Reopening at the latest version is a no-op (no error, no re-run).
    _reset_conn(db)
    db.connect()
    assert _user_version(db) == base + 2
