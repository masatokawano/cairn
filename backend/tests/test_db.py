import importlib
import json
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


def make_conv(db, source_id="c1", texts=None):
    from app.parsers.base import ParsedConversation, ParsedMessage
    texts = texts or [("user", "SQLiteのFTS5で日本語検索"), ("assistant", "trigramが使えます")]
    return ParsedConversation(
        source="chatgpt",
        source_id=source_id,
        title="テスト会話",
        messages=[ParsedMessage(role=r, text=t, created_at="2025-01-01T00:00:00Z") for r, t in texts],
        created_at="2025-01-01T00:00:00Z",
        updated_at="2025-01-01T00:10:00Z",
    )


def test_insert_and_diff_import(db):
    conv = make_conv(db)
    assert db.upsert_conversations([conv]) == {"inserted": 1, "updated": 0, "skipped": 0}
    # same content → skipped
    assert db.upsert_conversations([conv]) == {"inserted": 0, "updated": 0, "skipped": 1}
    # changed content → updated, messages replaced
    conv2 = make_conv(db, texts=[("user", "SQLiteのFTS5で日本語検索"), ("assistant", "trigram推奨"), ("user", "ありがとう")])
    assert db.upsert_conversations([conv2]) == {"inserted": 0, "updated": 1, "skipped": 0}
    full = db.get_conversation(1)
    assert len(full["messages"]) == 3


def test_search_fts_japanese(db):
    db.upsert_conversations([make_conv(db)])
    results = db.search("日本語検索")
    assert len(results) == 1
    assert "[[" in results[0]["snippet"]
    assert results[0]["title"] == "テスト会話"


def test_search_short_query_like_fallback(db):
    db.upsert_conversations([make_conv(db)])
    results = db.search("検索")  # 2 chars → LIKE fallback
    assert len(results) == 1
    assert "[[検索]]" in results[0]["snippet"]


def test_search_multi_term_and(db):
    db.upsert_conversations([make_conv(db)])
    assert len(db.search("FTS5 日本語検索")) == 1
    assert len(db.search("FTS5 存在しない単語")) == 0


def test_search_source_filter(db):
    db.upsert_conversations([make_conv(db)])
    assert len(db.search("日本語検索", source="chatgpt")) == 1
    assert len(db.search("日本語検索", source="claude")) == 0


def test_search_groups_by_conversation(db):
    conv = make_conv(db, texts=[("user", "日本語検索について"), ("assistant", "日本語検索はこうします")])
    db.upsert_conversations([conv])
    results = db.search("日本語検索")
    assert len(results) == 1
    assert results[0]["hit_count"] == 2


def test_search_paging_db_side(db):
    convs = [
        make_conv(db, f"c{i}", texts=[("user", f"日本語検索のサンプル {i}")])
        for i in range(5)
    ]
    db.upsert_conversations(convs)
    page1 = db.search("日本語検索", limit=2, offset=0)
    page2 = db.search("日本語検索", limit=2, offset=2)
    page3 = db.search("日本語検索", limit=2, offset=4)
    assert [len(page1), len(page2), len(page3)] == [2, 2, 1]
    ids = [r["conversation_id"] for r in page1 + page2 + page3]
    assert len(set(ids)) == 5  # no duplicates across pages


def test_stats_and_listing(db):
    db.upsert_conversations([make_conv(db, "c1"), make_conv(db, "c2")])
    s = db.stats()
    assert s["sources"][0]["conversations"] == 2
    listing = db.list_conversations()
    assert len(listing) == 2
    assert listing[0]["message_count"] == 2


def test_file_state_roundtrip(db):
    assert db.file_state("/tmp/x") is None
    db.record_file_state("/tmp/x", 123.0, 456)
    assert db.file_state("/tmp/x") == (123.0, 456)
    db.record_file_state("/tmp/x", 124.0, 457)
    assert db.file_state("/tmp/x") == (124.0, 457)
