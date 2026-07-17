"""Tests for the recent-activity listing (db.list_items / GET /api/items) and
the single-item detail (db.get_item_by_id / GET /api/items/{id}).

list_items merges conversations with external items so items-only sources
(x/facebook/karakeep/zotero/obsidian) get a browsable empty-query list; the
conversation-mirror rows in items must never duplicate the conversations
branch, and NULL-dated rows (X likes/bookmarks) sort last."""
import importlib

import pytest
from fastapi.testclient import TestClient


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


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CAIRN_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("CAIRN_CLAUDE_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("CAIRN_CODEX_DIR", str(tmp_path / "codex"))
    from app import cli_sync, db, main
    importlib.reload(db)
    importlib.reload(cli_sync)
    importlib.reload(main)
    yield TestClient(main.app, base_url="http://127.0.0.1")
    conn = getattr(db._local, "conn", None)
    if conn:
        conn.close()
        db._local.conn = None


def make_conv(source_id="c1", *, title="会話", text="本文", created_at="2025-02-01T00:00:00Z",
              updated_at="2025-02-01T00:10:00Z"):
    from app.parsers.base import ParsedConversation, ParsedMessage
    return ParsedConversation(
        source="chatgpt", source_id=source_id, title=title,
        messages=[ParsedMessage(role="user", text=text, created_at=created_at)],
        created_at=created_at, updated_at=updated_at,
    )


def _item(external_id, *, title="タイトル", url="https://example.com/a",
          created_at="2025-01-01T00:00:00Z", updated_at="2025-01-01T00:00:00Z",
          meta=None):
    return {
        "external_id": external_id, "title": title, "url": url,
        "created_at": created_at, "updated_at": updated_at,
        "meta": meta or {},
    }


# --- db.list_items -----------------------------------------------------------

def test_merge_order_and_row_shapes(db):
    db.upsert_conversations([make_conv(updated_at="2025-02-01T00:00:00Z")])
    db.upsert_items("karakeep", "bookmark",
                    [_item("b1", updated_at="2025-03-01T00:00:00Z")])
    db.upsert_items("zotero", "reference",
                    [_item("r1", url=None, updated_at="2025-01-01T00:00:00Z")])

    rows = db.list_items()
    assert [r["kind"] for r in rows] == ["bookmark", "conversation", "reference"]

    bm, conv, _ = rows
    assert bm["conversation_id"] is None
    assert bm["item_id"] is not None
    assert bm["url"] == "https://example.com/a"
    assert bm["message_count"] is None
    assert conv["conversation_id"] is not None
    assert conv["url"] is None
    assert conv["message_count"] == 1
    assert conv["external_id"] == "c1"


def test_conversation_mirror_not_duplicated(db):
    """Every conversation upsert mirrors into items(kind='conversation');
    the merged list must show it exactly once."""
    db.upsert_conversations([make_conv()])
    mirror = db.connect().execute(
        "SELECT COUNT(*) FROM items WHERE kind='conversation'"
    ).fetchone()[0]
    assert mirror == 1  # precondition: the mirror row exists
    rows = db.list_items()
    assert len(rows) == 1
    assert rows[0]["kind"] == "conversation"


def test_null_dates_sort_last_and_are_excluded_by_date_filter(db):
    """X likes/bookmarks carry no created_at/updated_at; they must sort last
    and drop out once a date window is set (same as _keyword_item_rows)."""
    db.upsert_items("x", "bookmark",
                    [_item("like1", created_at=None, updated_at=None)])
    db.upsert_items("x", "social_post",
                    [_item("post1", updated_at="2025-01-15T00:00:00Z",
                           created_at="2025-01-15T00:00:00Z")])

    rows = db.list_items()
    assert [r["external_id"] for r in rows] == ["post1", "like1"]

    rows = db.list_items(after="2025-01-01")
    assert [r["external_id"] for r in rows] == ["post1"]


def test_kinds_filter(db):
    db.upsert_conversations([make_conv()])
    db.upsert_items("x", "social_post", [_item("p1")])
    db.upsert_items("karakeep", "bookmark", [_item("b1")])

    assert [r["kind"] for r in db.list_items(kinds=["social_post"])] == ["social_post"]
    assert [r["kind"] for r in db.list_items(kinds=["conversation"])] == ["conversation"]
    kinds = {r["kind"] for r in db.list_items(kinds=["conversation", "bookmark"])}
    assert kinds == {"conversation", "bookmark"}


def test_source_and_date_window(db):
    db.upsert_conversations([make_conv(updated_at="2025-02-01T00:10:00Z")])
    db.upsert_items("x", "social_post",
                    [_item("p1", updated_at="2025-03-01T00:00:00Z")])

    assert [r["source"] for r in db.list_items(source="x")] == ["x"]
    assert [r["source"] for r in db.list_items(source="chatgpt")] == ["chatgpt"]
    # window spanning only the conversation
    rows = db.list_items(after="2025-02-01", before="2025-02-28T23:59:59Z")
    assert [r["kind"] for r in rows] == ["conversation"]


def test_limit_offset_paging_is_deterministic(db):
    # same timestamp on purpose: the item_id DESC tiebreak must make paging stable
    db.upsert_items("x", "social_post",
                    [_item(f"p{i}", updated_at="2025-01-01T00:00:00Z")
                     for i in range(5)])
    page1 = db.list_items(limit=2, offset=0)
    page2 = db.list_items(limit=2, offset=2)
    page3 = db.list_items(limit=2, offset=4)
    ids = [r["item_id"] for r in page1 + page2 + page3]
    assert len(ids) == 5
    assert ids == sorted(ids, reverse=True)


def test_url_gate(db):
    db.upsert_items("karakeep", "bookmark",
                    [_item("evil", url="javascript:alert(1)")])
    rows = db.list_items()
    assert rows[0]["url"] is None


# --- db.get_item_by_id --------------------------------------------------------

def test_get_item_by_id_external(db):
    db.upsert_items("x", "social_post",
                    [_item("p1", title="投稿", meta={"text": "本文テキスト"})])
    item_id = db.list_items()[0]["item_id"]
    it = db.get_item_by_id(item_id)
    assert it["kind"] == "social_post"
    assert it["conversation_id"] is None
    assert "本文テキスト" in it["body"]


def test_get_item_by_id_conversation_resolves_conv_id(db):
    db.upsert_conversations([make_conv()])
    row = db.list_items()[0]
    it = db.get_item_by_id(row["item_id"])
    assert it["kind"] == "conversation"
    assert it["conversation_id"] == row["conversation_id"]
    assert it["body"] is None


def test_get_item_by_id_missing(db):
    assert db.get_item_by_id(999999) is None


# --- HTTP endpoints -----------------------------------------------------------

def test_api_items_lists_merged(client):
    from app import db
    db.upsert_conversations([make_conv()])
    db.upsert_items("x", "social_post",
                    [_item("p1", updated_at="2025-03-01T00:00:00Z")])
    r = client.get("/api/items")
    assert r.status_code == 200
    kinds = [row["kind"] for row in r.json()["results"]]
    assert kinds == ["social_post", "conversation"]


def test_api_items_kinds_param_validates(client):
    r = client.get("/api/items", params={"kinds": "conversation,bogus"})
    assert r.status_code == 422
    assert "bogus" in r.json()["detail"]


def test_api_items_date_filter(client):
    from app import db
    db.upsert_items("x", "social_post",
                    [_item("p1", updated_at="2025-03-01T00:00:00Z")])
    r = client.get("/api/items", params={"after": "2099-01-01"})
    assert r.status_code == 200
    assert r.json()["results"] == []


def test_api_item_detail(client):
    from app import db
    db.upsert_items("x", "social_post",
                    [_item("p1", title="投稿", meta={"text": "本文テキスト"})])
    item_id = db.list_items()[0]["item_id"]
    r = client.get(f"/api/items/{item_id}")
    assert r.status_code == 200
    body = r.json()
    assert body["title"] == "投稿"
    assert body["url"] == "https://example.com/a"
    assert "本文テキスト" in body["body"]


def test_api_item_detail_missing(client):
    r = client.get("/api/items/999999")
    assert r.status_code == 404
