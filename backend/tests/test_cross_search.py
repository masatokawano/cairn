"""M2 cross-source search (DESIGN.md §7 M2).

Core cases: external items are chunked into kind='item_text' rows indexed by
chunks_fts; keyword/semantic/hybrid search returns bookmarks and the
conversations that discussed them in ONE result list; kinds/source filters
narrow it; conversation-only archives keep their pre-M2 results (regression).
"""
import importlib
import json

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


def make_conv(source_id, text, *, title="会話"):
    from app.parsers.base import ParsedConversation, ParsedMessage
    return ParsedConversation(
        source="chatgpt", source_id=source_id, title=title,
        messages=[ParsedMessage(role="user", text=text,
                                created_at="2026-06-01T00:00:00Z")],
        created_at="2026-06-01T00:00:00Z", updated_at="2026-06-01T00:10:00Z",
    )


def add_bookmark(db, external_id, *, title, url="https://example.com/x",
                 updated_at="2026-06-10T00:00:00Z", **meta):
    stats = db.upsert_items("karakeep", "bookmark", [{
        "external_id": external_id, "title": title, "url": url,
        "created_at": updated_at, "updated_at": updated_at, "meta": meta,
    }])
    db.rechunk_items(stats["changed_ids"], force=True)
    return stats


# --- rechunk_items ------------------------------------------------------------

def test_rechunk_items_creates_item_text_chunks(db):
    add_bookmark(db, "bm-1", title="Rust の所有権入門",
                 description="borrow checker の解説記事", tags=["rust", "memory"])
    rows = db.connect().execute(
        "SELECT ch.kind, ch.message_id, ch.conversation_id, ch.text, i.kind AS item_kind"
        " FROM chunks ch JOIN items i ON i.id = ch.item_id WHERE i.external_id='bm-1'"
    ).fetchall()
    assert len(rows) >= 1
    assert all(r["kind"] == "item_text" for r in rows)
    assert all(r["message_id"] is None and r["conversation_id"] is None for r in rows)
    joined = "\n".join(r["text"] for r in rows)
    assert "所有権" in joined and "borrow checker" in joined and "rust" in joined


def test_rechunk_items_skip_and_force(db):
    add_bookmark(db, "bm-1", title="タイトル", description="本文")
    item_id = db.connect().execute(
        "SELECT id FROM items WHERE external_id='bm-1'").fetchone()[0]
    again = db.rechunk_items([item_id], force=False)
    assert again == {"items": 0, "chunks": 0, "skipped": 1, "chunk_ids": []}
    forced = db.rechunk_items([item_id], force=True)
    assert forced["items"] == 1 and forced["chunks"] >= 1
    # no duplicates after force
    n = db.connect().execute(
        "SELECT COUNT(*) FROM chunks WHERE item_id=?", (item_id,)).fetchone()[0]
    assert n == forced["chunks"]


def test_conversation_items_never_item_chunked(db):
    db.upsert_conversations([make_conv("c1", "本文テキスト")])
    stats = db.rechunk_items(force=True)
    assert stats["items"] == 0
    n = db.connect().execute(
        "SELECT COUNT(*) FROM chunks WHERE kind='item_text'").fetchone()[0]
    assert n == 0


def test_chunks_fts_stays_in_sync(db):
    add_bookmark(db, "bm-1", title="特殊語彙テスト", description="quineology 記事")
    hit = db.connect().execute(
        "SELECT COUNT(*) FROM chunks_fts WHERE chunks_fts MATCH ?", ("quineology",)
    ).fetchone()[0]
    assert hit == 1
    # re-chunk (delete + insert) keeps the FTS mirror consistent via triggers
    item_id = db.connect().execute(
        "SELECT id FROM items WHERE external_id='bm-1'").fetchone()[0]
    db.rechunk_items([item_id], force=True)
    hit = db.connect().execute(
        "SELECT COUNT(*) FROM chunks_fts WHERE chunks_fts MATCH ?", ("quineology",)
    ).fetchone()[0]
    assert hit == 1
    # message chunks never leak into chunks_fts
    db.upsert_conversations([make_conv("c1", "quineology を会話でも言及")])
    hit = db.connect().execute(
        "SELECT COUNT(*) FROM chunks_fts WHERE chunks_fts MATCH ?", ("quineology",)
    ).fetchone()[0]
    assert hit == 1


# --- keyword search across sources ---------------------------------------------

def test_bookmark_and_conversation_in_one_result_list(db):
    """M2 完了条件の fixture 版: Karakeep に保存した記事とその話をした会話が
    同一クエリの結果に並ぶ。"""
    db.upsert_conversations([make_conv(
        "c1", "sqlite-vec のベクトル検索を試した", title="ベクトル検索の会話")])
    add_bookmark(db, "bm-1", title="sqlite-vec 入門記事",
                 url="https://example.com/sqlite-vec")
    results = db.search("sqlite-vec")
    kinds = {(r["kind"], r["source"]) for r in results}
    assert ("conversation", "chatgpt") in kinds
    assert ("bookmark", "karakeep") in kinds
    bm = next(r for r in results if r["kind"] == "bookmark")
    assert bm["url"] == "https://example.com/sqlite-vec"
    assert bm["conversation_id"] is None and bm["role"] is None
    assert bm["matched_keywords"]  # snippet highlights present
    conv = next(r for r in results if r["kind"] == "conversation")
    assert conv["item_id"] is not None  # cross-source key on conversation rows


def test_kinds_filter(db):
    db.upsert_conversations([make_conv("c1", "typescript の型パズル")])
    add_bookmark(db, "bm-1", title="typescript 記事")
    only_bm = db.search("typescript", kinds=["bookmark"])
    assert [r["kind"] for r in only_bm] == ["bookmark"]
    only_conv = db.search("typescript", kinds=["conversation"])
    assert [r["kind"] for r in only_conv] == ["conversation"]
    both = db.search("typescript", kinds=["conversation", "bookmark"])
    assert {r["kind"] for r in both} == {"conversation", "bookmark"}


def test_source_filter_selects_item_source(db):
    db.upsert_conversations([make_conv("c1", "pandas 使い方")])
    add_bookmark(db, "bm-1", title="pandas チートシート")
    res = db.search("pandas", source="karakeep")
    assert [r["source"] for r in res] == ["karakeep"]


def test_conversation_only_archive_results_unchanged(db):
    """Regression: with no external items, keyword results keep the pre-M2
    ordering/paging (SQL path) and now carry the M2 fields."""
    db.upsert_conversations([
        make_conv("c1", "regression テスト対象の本文"),
        make_conv("c2", "regression 二件目の本文"),
    ])
    res = db.search("regression")
    assert len(res) == 2
    assert all(r["kind"] == "conversation" for r in res)
    assert all(r["url"] is None for r in res)
    assert all(r["item_id"] is not None for r in res)


def test_like_fallback_covers_items(db):
    """<3-char terms use the LIKE path; external items must still surface."""
    add_bookmark(db, "bm-1", title="Go 言語のプロファイラ")
    res = db.search("Go")
    assert [r["kind"] for r in res] == ["bookmark"]


# --- semantic / hybrid across sources -------------------------------------------

class FixtureProvider:
    """Deterministic 8-dim provider (mirrors test_embedding's approach)."""
    name = "fixture"
    model = "unit"
    dimension = 8

    def _vec(self, text: str) -> bytes:
        import hashlib as h
        import math
        import struct
        raw = h.sha256(text[:16].encode()).digest()[:8]
        vals = [b / 255.0 for b in raw]
        norm = math.sqrt(sum(v * v for v in vals)) or 1.0
        return struct.pack("<8f", *(v / norm for v in vals))

    def embed_passages(self, texts):
        return [self._vec(t) for t in texts]

    def embed_query(self, text):
        return self._vec(text)


def _embed_all(db):
    provider = FixtureProvider()
    db.embed_chunks(provider, only_missing=True)
    return provider


def test_semantic_search_spans_kinds(db, monkeypatch):
    monkeypatch.setenv("CAIRN_VECTOR_INDEX", "numpy")
    db.upsert_conversations([make_conv("c1", "unique-semantic-topic の議論")])
    add_bookmark(db, "bm-1", title="unique-semantic-topic の記事")
    provider = _embed_all(db)
    res = db.search("unique-semantic-topic", mode="semantic", provider=provider, limit=10)
    kinds = {r["kind"] for r in res}
    assert kinds == {"conversation", "bookmark"}
    bm = next(r for r in res if r["kind"] == "bookmark")
    assert bm["match_reason"] == "semantic"
    assert bm["semantic_score"] is not None
    assert bm["role"] is None and bm["message_id"] is None


def test_hybrid_merges_by_item(db, monkeypatch):
    monkeypatch.setenv("CAIRN_VECTOR_INDEX", "numpy")
    db.upsert_conversations([make_conv("c1", "hybridtopic を検討した会話")])
    add_bookmark(db, "bm-1", title="hybridtopic まとめ記事")
    provider = _embed_all(db)
    res = db.search("hybridtopic", mode="hybrid", provider=provider, limit=10)
    by_kind = {r["kind"]: r for r in res}
    assert set(by_kind) == {"conversation", "bookmark"}
    # keyword+semantic both fire for the bookmark → match_reason "both"
    assert by_kind["bookmark"]["match_reason"] == "both"


def test_semantic_kinds_filter(db, monkeypatch):
    monkeypatch.setenv("CAIRN_VECTOR_INDEX", "numpy")
    db.upsert_conversations([make_conv("c1", "filtertopic の議論")])
    add_bookmark(db, "bm-1", title="filtertopic 記事")
    provider = _embed_all(db)
    res = db.search("filtertopic", mode="semantic", provider=provider,
                    kinds=["bookmark"], limit=10)
    assert res and all(r["kind"] == "bookmark" for r in res)


# --- migration v12 ---------------------------------------------------------------

def test_migration_v12_rebuilds_chunks_preserving_ids(db, tmp_path, monkeypatch):
    """Seed a v11-shape DB (NOT NULL chunks, no chunks_fts), reopen, and
    verify: chunks rebuilt with ids intact, embeddings NOT cascade-deleted
    (the FK-off rebuild), chunks_fts present, user_version = 12."""
    import sqlite3
    from tests.schema_shapes import downgrade_chunks_pre_v11

    # Build current DB with one conversation + chunk + fake embedding.
    db.upsert_conversations([make_conv("c1", "migration 検証本文")])
    conn = db.connect()
    chunk_id = conn.execute("SELECT id FROM chunks").fetchone()[0]
    with conn:
        conn.execute(
            "INSERT INTO embeddings (chunk_id, provider, model, dimension, vector, created_at)"
            " VALUES (?, 'fixture', 'unit', 2, x'0000803f0000803f', '2026-01-01')",
            (chunk_id,),
        )
    # Downgrade to v11 shape: pre-v11 chunks + re-add item_id w/o CHECK.
    # (The helper manages its own transaction — FK pragma needs to be live.)
    downgrade_chunks_pre_v11(conn)
    with conn:
        conn.execute("ALTER TABLE chunks ADD COLUMN item_id INTEGER REFERENCES items(id)")
        conn.execute("""CREATE TABLE items (
            id INTEGER PRIMARY KEY, kind TEXT NOT NULL, source TEXT NOT NULL,
            external_id TEXT NOT NULL, title TEXT, url TEXT, url_norm TEXT,
            doi TEXT, created_at TEXT, updated_at TEXT, content_hash TEXT,
            meta TEXT, UNIQUE (source, external_id))""")
        conn.execute("""CREATE TABLE item_links (
            a_id INTEGER NOT NULL REFERENCES items(id),
            b_id INTEGER NOT NULL REFERENCES items(id),
            link_via TEXT NOT NULL CHECK (link_via IN ('url','doi','github')),
            PRIMARY KEY (a_id, b_id, link_via), CHECK (a_id < b_id))""")
        conn.execute("""CREATE TABLE sync_state (
            source TEXT PRIMARY KEY, cursor TEXT NOT NULL,
            synced_at TEXT NOT NULL, last_error TEXT)""")
        conn.execute(
            "INSERT INTO items (kind, source, external_id, title) "
            "SELECT 'conversation', source, source_id, title FROM conversations")
        conn.execute("UPDATE chunks SET item_id = 1")
        conn.execute("PRAGMA user_version = 11")
    conn.close()
    db._local.conn = None

    conn = db.connect()  # reopen → migration 12
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 12
    # chunk survived with the same id; NOT NULL is gone (CHECK in place)
    row = conn.execute("SELECT id, message_id, item_id FROM chunks").fetchone()
    assert row["id"] == chunk_id and row["item_id"] == 1
    # embeddings survived the FK-off rebuild
    assert conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0] == 1
    # chunks_fts + triggers exist; NULL-message insert now legal
    with conn:
        conn.execute(
            "INSERT INTO chunks (message_id, conversation_id, idx, start_offset,"
            " end_offset, text, kind, chunking_version, created_at, item_id)"
            " VALUES (NULL, NULL, 0, 0, 4, 'itemtext', 'item_text', 'v1', 'now', 1)")
    assert conn.execute(
        "SELECT COUNT(*) FROM chunks_fts WHERE chunks_fts MATCH 'itemtext'"
    ).fetchone()[0] == 1
