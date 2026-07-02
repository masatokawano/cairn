"""Tests for the M0 items registry maintained by the ingest path.

Every conversation upsert mirrors into items(kind='conversation'); every chunk
carries the corresponding items.id; skip path (hash-unchanged) is a no-op on
items; rechunk self-heals a missing items row."""
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


def make_conv(source_id="c1", *, title="会話", text="本文", created_at="2025-01-01T00:00:00Z",
              updated_at="2025-01-01T00:10:00Z"):
    from app.parsers.base import ParsedConversation, ParsedMessage
    return ParsedConversation(
        source="chatgpt", source_id=source_id, title=title,
        messages=[ParsedMessage(role="user", text=text, created_at=created_at)],
        created_at=created_at, updated_at=updated_at,
    )


def _item_row(db, source="chatgpt", external_id="c1"):
    return db.connect().execute(
        "SELECT id, kind, title, created_at, updated_at, content_hash, meta"
        " FROM items WHERE source=? AND external_id=?",
        (source, external_id),
    ).fetchone()


def test_insert_creates_item_and_links_chunks(db):
    db.upsert_conversations([make_conv()])
    item = _item_row(db)
    assert item is not None
    assert item["kind"] == "conversation"
    assert item["title"] == "会話"

    chunk_item_ids = [r[0] for r in db.connect().execute(
        "SELECT item_id FROM chunks"
    ).fetchall()]
    assert chunk_item_ids
    assert all(cid == item["id"] for cid in chunk_item_ids)


def test_update_refreshes_item_fields(db):
    db.upsert_conversations([make_conv()])
    before = _item_row(db)

    updated = make_conv(title="改題", text="改稿", updated_at="2025-02-01T00:00:00Z")
    db.upsert_conversations([updated])
    after = _item_row(db)

    assert after["id"] == before["id"]
    assert after["title"] == "改題"
    assert after["updated_at"] == "2025-02-01T00:00:00Z"
    assert after["content_hash"] != before["content_hash"]

    # Chunks are regenerated on update (messages CASCADE) and still link.
    chunk_item_ids = {r[0] for r in db.connect().execute(
        "SELECT item_id FROM chunks"
    ).fetchall()}
    assert chunk_item_ids == {after["id"]}


def test_skip_path_does_not_touch_items(db):
    """content_hash unchanged → skip. items must be untouched (no churn on
    every sync)."""
    db.upsert_conversations([make_conv()])
    before = _item_row(db)

    stats = db.upsert_conversations([make_conv()])
    assert stats == {"inserted": 0, "updated": 0, "skipped": 1}

    after = _item_row(db)
    assert dict(after) == dict(before)


def test_rechunk_populates_and_self_heals_items(db):
    """rechunk_messages resolves item_id via the helper. If a legacy DB is
    missing its items row (raw-SQL fixtures, hand-edited state), the helper
    upserts one."""
    db.upsert_conversations([make_conv()])
    conn = db.connect()
    with conn:
        # Simulate legacy state: chunks written without item_id, items row gone.
        conn.execute("UPDATE chunks SET item_id = NULL")
        conn.execute("DELETE FROM items")
    # rechunk with force=True re-runs the chunk pipeline for existing versions.
    db.rechunk_messages(force=True)

    item = _item_row(db)
    assert item is not None, "rechunk must self-heal the missing items row"

    chunk_item_ids = [r[0] for r in db.connect().execute(
        "SELECT item_id FROM chunks"
    ).fetchall()]
    assert chunk_item_ids
    assert all(cid == item["id"] for cid in chunk_item_ids)


def test_kind_check_constraint_rejects_unknown_values(db):
    """DESIGN.md §4 kind is enumerated; malformed inserts must fail early."""
    db.connect()
    with pytest.raises(Exception):
        db.connect().execute(
            "INSERT INTO items (kind, source, external_id) VALUES ('bogus', 's', 'x')"
        )


def test_multiple_conversations_get_distinct_items(db):
    db.upsert_conversations([make_conv("c1"), make_conv("c2")])
    rows = db.connect().execute(
        "SELECT id, external_id FROM items ORDER BY external_id"
    ).fetchall()
    assert [r["external_id"] for r in rows] == ["c1", "c2"]
    assert rows[0]["id"] != rows[1]["id"]
