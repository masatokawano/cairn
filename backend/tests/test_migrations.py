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


def test_migration_v7_extraction_runs_table(db):
    """Migration 7 creates the extraction_runs table on a pre-v7 DB."""
    tables = [r[0] for r in db.connect().execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()]
    assert "extraction_runs" in tables
    cols = [r[1] for r in db.connect().execute("PRAGMA table_info(extraction_runs)").fetchall()]
    for expected in ("id", "kind", "scope", "provider", "model", "prompt_version",
                     "started_at", "completed_at", "status", "input_token_count",
                     "output_token_count", "retries", "warnings", "warning_summary", "error"):
        assert expected in cols, f"missing column: {expected}"


def test_migration_v7_extraction_runs_indexes(db):
    """extraction_runs has the expected indexes after migration 7."""
    indexes = [r[1] for r in db.connect().execute(
        "SELECT * FROM sqlite_master WHERE type='index' AND tbl_name='extraction_runs'"
    ).fetchall()]
    assert any("kind" in idx for idx in indexes)
    assert any("status" in idx for idx in indexes)


def _seed_v10_shape_db(path):
    """Build a v10-shape DB with raw SQL (no items/item_links/sync_state,
    no chunks.item_id), pre-populated with a conversation + message + chunk,
    and user_version stamped to 10. Mirrors the pattern in
    test_admin_migration.py's seed_legacy_rows() for reproducing pre-migration
    on-disk shapes without going through upsert_conversations()."""
    import sqlite3

    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE conversations (
            id INTEGER PRIMARY KEY,
            source TEXT NOT NULL,
            source_id TEXT NOT NULL,
            title TEXT NOT NULL,
            created_at TEXT,
            updated_at TEXT,
            content_hash TEXT NOT NULL,
            meta TEXT NOT NULL DEFAULT '{}',
            UNIQUE(source, source_id)
        );
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY,
            conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
            idx INTEGER NOT NULL,
            role TEXT NOT NULL,
            text TEXT NOT NULL,
            created_at TEXT,
            source_message_id TEXT
        );
        CREATE TABLE chunks (
            id INTEGER PRIMARY KEY,
            message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
            conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
            idx INTEGER NOT NULL,
            start_offset INTEGER NOT NULL,
            end_offset INTEGER NOT NULL,
            text TEXT NOT NULL,
            kind TEXT NOT NULL DEFAULT 'message_text',
            chunking_version TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        INSERT INTO conversations (source, source_id, title, created_at, updated_at, content_hash, meta)
        VALUES ('chatgpt', 'c1', 'v10 会話', '2025-01-01T00:00:00Z', '2025-01-01T00:10:00Z', 'hash1', '{}');
        INSERT INTO messages (conversation_id, idx, role, text, created_at)
        VALUES (1, 0, 'user', '本文', '2025-01-01T00:00:00Z');
        INSERT INTO chunks (message_id, conversation_id, idx, start_offset, end_offset, text, chunking_version, created_at)
        VALUES (1, 1, 0, 0, 2, '本文', 'v1', '2025-01-01T00:00:00Z');
        PRAGMA user_version = 10;
    """)
    conn.commit()
    conn.close()


def test_migration_v11_backfills_items_and_chunk_item_id(db, tmp_path):
    """A pre-v11 (v10-shape) DB gets items/item_links/sync_state created,
    every conversation backfilled into items, and every chunk's item_id
    resolved — with a premigrate backup taken first."""
    db_path = tmp_path / "test.db"
    _reset_conn(db)
    _seed_v10_shape_db(str(db_path))

    db.connect()
    assert _user_version(db) == db._SCHEMA_VERSION

    tables = {r[0] for r in db.connect().execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    assert {"items", "item_links", "sync_state"} <= tables

    items = db.connect().execute(
        "SELECT kind, source, external_id, title FROM items"
    ).fetchall()
    assert len(items) == 1
    assert items[0]["kind"] == "conversation"
    assert items[0]["source"] == "chatgpt"
    assert items[0]["external_id"] == "c1"
    assert items[0]["title"] == "v10 会話"

    chunk_item_ids = [r[0] for r in db.connect().execute(
        "SELECT item_id FROM chunks"
    ).fetchall()]
    assert len(chunk_item_ids) == 1
    assert chunk_item_ids[0] is not None

    # Verify the chunk resolves to the same items row backfilled from conversations.
    item_id = db.connect().execute(
        "SELECT id FROM items WHERE source='chatgpt' AND external_id='c1'"
    ).fetchone()[0]
    assert chunk_item_ids[0] == item_id

    backups = glob.glob(str(tmp_path / f"*.premigrate-v10-to-v{db._SCHEMA_VERSION}-*"))
    assert len(backups) == 1
    assert oct(os.stat(backups[0]).st_mode & 0o777) == "0o600"


def test_migration_v11_reopen_is_noop(db, tmp_path):
    """Reopening a v11 DB does not re-run migration 11 or create another backup."""
    db_path = tmp_path / "test.db"
    _reset_conn(db)
    _seed_v10_shape_db(str(db_path))
    db.connect()
    assert _user_version(db) == db._SCHEMA_VERSION
    _reset_conn(db)

    db.connect()
    assert _user_version(db) == db._SCHEMA_VERSION
    backups = glob.glob(str(tmp_path / f"*.premigrate-v10-to-v{db._SCHEMA_VERSION}-*"))
    assert len(backups) == 1  # still just the one from the initial migration


def test_migration_v11_fresh_vs_migrated_schema_equivalence(db, tmp_path):
    """A fresh DB (built straight from _SCHEMA) and a migrated v10->v11 DB
    must end up with the same chunks columns and the same set of indexes.
    Guards against _SCHEMA and _MIGRATIONS drifting apart."""
    fresh_cols = [r[1] for r in db.connect().execute("PRAGMA table_info(chunks)")]
    fresh_indexes = {r[0] for r in db.connect().execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='chunks'"
    ).fetchall()}

    migrated_path = tmp_path / "migrated.db"
    _seed_v10_shape_db(str(migrated_path))
    import importlib
    import os as _os
    _os.environ["CAIRN_DB"] = str(migrated_path)
    from app import db as db_module
    importlib.reload(db_module)
    db_module.connect()
    migrated_cols = [r[1] for r in db_module.connect().execute("PRAGMA table_info(chunks)")]
    migrated_indexes = {r[0] for r in db_module.connect().execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='chunks'"
    ).fetchall()}
    conn = getattr(db_module._local, "conn", None)
    if conn:
        conn.close()
        db_module._local.conn = None

    assert fresh_cols == migrated_cols
    assert fresh_indexes == migrated_indexes


def _seed_v12_shape_db(path):
    """Build a v12-shape DB with raw SQL: items with the pre-v13 CHECK (no
    'social_post'), item_links, and a chunks row anchored to an item — so the
    v13 items rebuild can be verified to preserve ids that other tables
    reference. user_version stamped to 12."""
    import sqlite3

    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE conversations (
            id INTEGER PRIMARY KEY,
            source TEXT NOT NULL,
            source_id TEXT NOT NULL,
            title TEXT NOT NULL,
            created_at TEXT,
            updated_at TEXT,
            content_hash TEXT NOT NULL,
            meta TEXT NOT NULL DEFAULT '{}',
            UNIQUE(source, source_id)
        );
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY,
            conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
            idx INTEGER NOT NULL,
            role TEXT NOT NULL,
            text TEXT NOT NULL,
            created_at TEXT,
            source_message_id TEXT
        );
        CREATE TABLE items (
            id INTEGER PRIMARY KEY,
            kind TEXT NOT NULL CHECK (kind IN ('conversation','bookmark','reference','note')),
            source TEXT NOT NULL,
            external_id TEXT NOT NULL,
            title TEXT,
            url TEXT,
            url_norm TEXT,
            doi TEXT,
            created_at TEXT,
            updated_at TEXT,
            content_hash TEXT,
            meta TEXT,
            UNIQUE (source, external_id)
        );
        CREATE INDEX idx_items_url_norm ON items(url_norm) WHERE url_norm IS NOT NULL;
        CREATE INDEX idx_items_doi      ON items(doi)      WHERE doi IS NOT NULL;
        CREATE INDEX idx_items_updated  ON items(kind, updated_at);
        CREATE TABLE item_links (
            a_id     INTEGER NOT NULL REFERENCES items(id),
            b_id     INTEGER NOT NULL REFERENCES items(id),
            link_via TEXT NOT NULL CHECK (link_via IN ('url','doi','github')),
            PRIMARY KEY (a_id, b_id, link_via),
            CHECK (a_id < b_id)
        );
        CREATE TABLE sync_state (
            source     TEXT PRIMARY KEY,
            cursor     TEXT NOT NULL,
            synced_at  TEXT NOT NULL,
            last_error TEXT
        );
        CREATE TABLE chunks (
            id INTEGER PRIMARY KEY,
            message_id INTEGER REFERENCES messages(id) ON DELETE CASCADE,
            conversation_id INTEGER REFERENCES conversations(id) ON DELETE CASCADE,
            idx INTEGER NOT NULL,
            start_offset INTEGER NOT NULL,
            end_offset INTEGER NOT NULL,
            text TEXT NOT NULL,
            kind TEXT NOT NULL DEFAULT 'message_text',
            chunking_version TEXT NOT NULL,
            created_at TEXT NOT NULL,
            item_id INTEGER REFERENCES items(id),
            CHECK ((message_id IS NOT NULL AND conversation_id IS NOT NULL) OR item_id IS NOT NULL)
        );
        INSERT INTO items (kind, source, external_id, title, url, url_norm, content_hash, meta)
        VALUES ('bookmark', 'karakeep', 'kb1', 'ブックマーク', 'https://example.com/a',
                'https://example.com/a', 'h1', '{}');
        INSERT INTO items (kind, source, external_id, title, url, url_norm, content_hash, meta)
        VALUES ('reference', 'zotero', 'zt1', '文献', 'https://example.com/a',
                'https://example.com/a', 'h2', '{}');
        INSERT INTO item_links (a_id, b_id, link_via) VALUES (1, 2, 'url');
        INSERT INTO chunks (message_id, conversation_id, idx, start_offset, end_offset,
                            text, kind, chunking_version, created_at, item_id)
        VALUES (NULL, NULL, 0, 0, 6, 'ブックマーク', 'item_text', 'v1', '2025-01-01T00:00:00Z', 1);
        PRAGMA user_version = 12;
    """)
    conn.commit()
    conn.close()


def test_migration_v13_widens_items_kind(db, tmp_path):
    """v12→v13 rebuilds items with the widened CHECK: rows and ids are
    preserved (item_links / chunks.item_id keep resolving), 'social_post'
    becomes insertable, invalid kinds stay rejected, FK integrity is clean."""
    import sqlite3

    db_path = tmp_path / "test.db"
    _reset_conn(db)
    _seed_v12_shape_db(str(db_path))

    conn = db.connect()
    assert _user_version(db) == db._SCHEMA_VERSION

    # Rows and ids preserved verbatim.
    rows = conn.execute(
        "SELECT id, kind, source, external_id, url_norm FROM items ORDER BY id"
    ).fetchall()
    assert [(r["id"], r["kind"], r["source"], r["external_id"]) for r in rows] == [
        (1, "bookmark", "karakeep", "kb1"),
        (2, "reference", "zotero", "zt1"),
    ]

    # References into items survived the rebuild.
    assert tuple(conn.execute("SELECT a_id, b_id FROM item_links").fetchone()) == (1, 2)
    assert conn.execute("SELECT item_id FROM chunks WHERE kind='item_text'").fetchone()[0] == 1
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []

    # The widened CHECK admits social_post…
    with conn:
        conn.execute(
            "INSERT INTO items (kind, source, external_id, title, meta)"
            " VALUES ('social_post', 'x', 'x:1', 'テスト投稿', '{}')"
        )
    assert conn.execute(
        "SELECT kind FROM items WHERE external_id='x:1'"
    ).fetchone()[0] == "social_post"

    # …and still rejects anything else.
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO items (kind, source, external_id) VALUES ('feed', 'x', 'x:2')"
        )

    # UNIQUE(source, external_id) also survived.
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO items (kind, source, external_id) VALUES ('bookmark', 'karakeep', 'kb1')"
        )


def test_migration_v13_fresh_vs_migrated_items_equivalence(db, tmp_path):
    """Fresh (_SCHEMA) and migrated (v12→v13) DBs must agree on the items
    table shape and indexes — guards _SCHEMA/_MIGRATIONS drift, same as the
    v11 equivalence test does for chunks."""
    fresh_cols = [r[1] for r in db.connect().execute("PRAGMA table_info(items)")]
    fresh_indexes = {r[0] for r in db.connect().execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='items'"
        " AND name NOT LIKE 'sqlite_autoindex%'"
    ).fetchall()}
    fresh_sql = db.connect().execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='items'"
    ).fetchone()[0]
    assert "social_post" in fresh_sql

    migrated_path = tmp_path / "migrated.db"
    _seed_v12_shape_db(str(migrated_path))
    import importlib
    import os as _os
    _os.environ["CAIRN_DB"] = str(migrated_path)
    from app import db as db_module
    importlib.reload(db_module)
    db_module.connect()
    migrated_cols = [r[1] for r in db_module.connect().execute("PRAGMA table_info(items)")]
    migrated_indexes = {r[0] for r in db_module.connect().execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='items'"
        " AND name NOT LIKE 'sqlite_autoindex%'"
    ).fetchall()}
    migrated_sql = db_module.connect().execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='items'"
    ).fetchone()[0]
    conn = getattr(db_module._local, "conn", None)
    if conn:
        conn.close()
        db_module._local.conn = None

    assert fresh_cols == migrated_cols
    assert fresh_indexes == migrated_indexes
    assert "social_post" in migrated_sql
