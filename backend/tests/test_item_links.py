"""item_links generation (M1, DESIGN.md §7 M1 完了条件 / D5).

The core case: a bookmark and a conversation that mention the same URL
(modulo tracking params / host aliases) end up linked via 'url'. Plus doi
and github key spaces, idempotent rebuild, and the a_id < b_id invariant.
"""
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


def make_conv(source_id, text, *, title="会話"):
    from app.parsers.base import ParsedConversation, ParsedMessage
    return ParsedConversation(
        source="chatgpt", source_id=source_id, title=title,
        messages=[ParsedMessage(role="user", text=text,
                                created_at="2026-06-01T00:00:00Z")],
        created_at="2026-06-01T00:00:00Z", updated_at="2026-06-01T00:10:00Z",
    )


def add_bookmark(db, external_id, url, **meta):
    from app.core import urlnorm
    norm = urlnorm.normalize_url(url)
    return db.upsert_items("karakeep", "bookmark", [{
        "external_id": external_id, "title": external_id, "url": url,
        "url_norm": norm, "doi": urlnorm.normalize_doi(norm),
        "created_at": "2026-06-10T00:00:00Z", "updated_at": "2026-06-10T00:00:00Z",
        "meta": meta,
    }])


def links(db):
    return [tuple(r) for r in db.connect().execute(
        "SELECT a_id, b_id, link_via FROM item_links ORDER BY a_id, b_id, link_via"
    ).fetchall()]


def item_id(db, source, external_id):
    return db.connect().execute(
        "SELECT id FROM items WHERE source=? AND external_id=?",
        (source, external_id),
    ).fetchone()[0]


def test_bookmark_and_conversation_share_url(db):
    """M1 完了条件: 同一 URL の bookmark と会話が item_links で結ばれる。
    The conversation carries tracking junk and the www. host variant —
    linking must survive normalisation."""
    db.upsert_conversations([make_conv(
        "conv-1", "この記事が面白い: https://www.example.com/article?utm_source=tw&fbclid=x を参照")])
    add_bookmark(db, "bm-1", "https://example.com/article")
    result = db.rebuild_item_links()

    assert result["url"] == 1
    conv_item = item_id(db, "chatgpt", "conv-1")
    bm_item = item_id(db, "karakeep", "bm-1")
    assert links(db) == [(min(conv_item, bm_item), max(conv_item, bm_item), "url")]


def test_doi_links_reference_to_bookmark(db):
    """Zotero reference (DOI field) ↔ Karakeep bookmark of the doi.org URL."""
    db.upsert_items("zotero", "reference", [{
        "external_id": "KEY1", "title": "論文", "url": None, "url_norm": None,
        "doi": "10.1038/s41586-023-06004-9",
        "created_at": None, "updated_at": None, "meta": {},
    }])
    add_bookmark(db, "bm-doi", "https://doi.org/10.1038/S41586-023-06004-9")
    result = db.rebuild_item_links()
    assert result["doi"] == 1
    assert [via for _a, _b, via in links(db)] == ["doi"]


def test_github_links_across_deep_paths(db):
    """Conversation mentions an issue URL; bookmark saves the tree URL —
    different url_norms, same repo key → 'github' link only."""
    db.upsert_conversations([make_conv(
        "conv-gh", "バグは https://github.com/anthropics/claude-code/issues/5 に報告した")])
    add_bookmark(db, "bm-gh", "https://github.com/anthropics/claude-code/tree/main")
    result = db.rebuild_item_links()
    assert result["github"] == 1
    assert result["url"] == 0
    assert [via for _a, _b, via in links(db)] == ["github"]


def test_conversations_sharing_url_are_linked(db):
    db.upsert_conversations([
        make_conv("conv-a", "参考: https://arxiv.org/abs/2310.06825"),
        make_conv("conv-b", "PDF はこちら https://arxiv.org/pdf/2310.06825v1.pdf"),
    ])
    result = db.rebuild_item_links()
    assert result["url"] == 1  # abs/pdf equivalence (§5.2)


def test_rebuild_is_idempotent_and_prunes_stale(db):
    db.upsert_conversations([make_conv("conv-1", "see https://example.com/a")])
    add_bookmark(db, "bm-1", "https://example.com/a")
    first = db.rebuild_item_links()
    second = db.rebuild_item_links()
    assert first == second
    assert len(links(db)) == first["total"]

    # bookmark moves to a different URL → stale link must disappear
    add_bookmark(db, "bm-1", "https://example.com/other")
    assert db.rebuild_item_links()["total"] == 0
    assert links(db) == []


def test_a_less_than_b_invariant(db):
    db.upsert_conversations([
        make_conv(f"conv-{i}", "同じ URL https://example.com/hub を共有") for i in range(3)
    ])
    result = db.rebuild_item_links()
    assert result["url"] == 3  # 3 items → 3 pairs
    for a_id, b_id, _via in links(db):
        assert a_id < b_id


def test_no_links_for_singleton_keys(db):
    db.upsert_conversations([make_conv("conv-1", "孤立 URL https://example.com/only")])
    add_bookmark(db, "bm-1", "https://example.com/elsewhere")
    assert db.rebuild_item_links()["total"] == 0


def test_stats_includes_items_breakdown(db):
    db.upsert_conversations([make_conv("conv-1", "hello")])
    add_bookmark(db, "bm-1", "https://example.com/x")
    s = db.stats()
    by_key = {(r["kind"], r["source"]): r["count"] for r in s["items"]}
    assert by_key[("conversation", "chatgpt")] == 1
    assert by_key[("bookmark", "karakeep")] == 1
    assert s["item_links"] == 0


def test_upsert_items_skip_then_update(db):
    """content_hash discipline: identical record → skip; changed meta →
    update in place (same items.id)."""
    add_bookmark(db, "bm-1", "https://example.com/x", tags=["a"])
    id_before = item_id(db, "karakeep", "bm-1")
    stats = add_bookmark(db, "bm-1", "https://example.com/x", tags=["a"])
    assert stats == {"inserted": 0, "updated": 0, "skipped": 1, "changed_ids": []}
    stats = add_bookmark(db, "bm-1", "https://example.com/x", tags=["a", "b"])
    assert stats["updated"] == 1
    assert item_id(db, "karakeep", "bm-1") == id_before
