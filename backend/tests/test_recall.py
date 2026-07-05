"""recall/ — related() and weekly digest (M4, DESIGN.md §5.3).

Covers the §5.3 contract: recent-item exclusion (exclude_days), the k cap,
light source diversity, per-item reasons (which activity text an item
reacted to), the keyword degradation when no embedding provider resolves,
and the weekly_activity section rules (7-day window, ≤10 items, conversation
noise rules shared with auto_lists, to-review 優先).
"""
import importlib
from datetime import datetime, timezone

import pytest

NOW = datetime(2026, 7, 4, 12, 0, 0, tzinfo=timezone.utc)

OLD = "2026-05-01T00:00:00Z"       # well past exclude_days
RECENT = "2026-06-28T00:00:00Z"    # inside exclude_days (6 days before NOW)
THIS_WEEK = "2026-07-02T00:00:00Z"


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


def add_item(db, source, kind, external_id, *, title, url=None,
             created=OLD, updated=OLD, **meta):
    stats = db.upsert_items(source, kind, [{
        "external_id": external_id, "title": title, "url": url,
        "created_at": created, "updated_at": updated, "meta": meta,
    }])
    # chunk so keyword search (chunks_fts) can see the item, like sync does
    db.rechunk_items(stats["changed_ids"], force=True)


def make_conv(db, source_id, *, title, text="本文", n_messages=4,
              updated=THIS_WEEK, source="chatgpt"):
    from app.parsers.base import ParsedConversation, ParsedMessage
    db.upsert_conversations([ParsedConversation(
        source=source, source_id=source_id, title=title,
        messages=[ParsedMessage(role="user", text=f"{text} {i}", created_at=updated)
                  for i in range(n_messages)],
        created_at=updated, updated_at=updated, meta={},
    )])


# --- related() ---------------------------------------------------------------

def test_related_excludes_recent_items(db):
    from app import recall
    add_item(db, "karakeep", "bookmark", "old", title="外部脳の設計メモ",
             tags=[], text="外部脳の設計メモ")
    add_item(db, "karakeep", "bookmark", "new", title="外部脳の新しい記事",
             created=RECENT, updated=RECENT, tags=[], text="外部脳の新しい記事")
    out = recall.related(["外部脳"], now=NOW)
    ids = {r["external_id"] for r in out}
    assert "old" in ids
    assert "new" not in ids


def test_related_caps_at_k_and_attaches_reason(db):
    from app import recall
    for i in range(8):
        add_item(db, "karakeep", "bookmark", f"bm-{i}",
                 title=f"外部脳の記事その{i}", text=f"外部脳の記事その{i}")
    out = recall.related(["外部脳"], k=3, now=NOW)
    assert len(out) == 3
    for r in out:
        assert r["reason"]["query"] == "外部脳"
        assert r["reason"]["match_reason"] == "keyword"


def test_related_source_diversity(db):
    """8 karakeep + 3 zotero candidates, k=6: karakeep must not take every
    slot (cap = ⌈k/2⌉ = 3 while other sources still have candidates)."""
    from app import recall
    for i in range(8):
        add_item(db, "karakeep", "bookmark", f"bm-{i}",
                 title=f"検索基盤の記事{i}", text=f"検索基盤の記事{i}")
    for i in range(3):
        add_item(db, "zotero", "reference", f"ref-{i}",
                 title=f"検索基盤の論文{i}", text=f"検索基盤の論文{i}")
    out = recall.related(["検索基盤"], k=6, now=NOW)
    sources = [r["source"] for r in out]
    assert len(out) == 6
    assert sources.count("karakeep") == 3
    assert sources.count("zotero") == 3


def test_related_overflow_fills_when_single_source(db):
    """With only one source available the cap is waived: k slots still fill."""
    from app import recall
    for i in range(6):
        add_item(db, "karakeep", "bookmark", f"bm-{i}",
                 title=f"単独ソースの記事{i}", text=f"単独ソースの記事{i}")
    out = recall.related(["単独ソース"], k=4, now=NOW)
    assert len(out) == 4
    assert {r["source"] for r in out} == {"karakeep"}


def test_related_multiple_queries_rrf_and_best_reason(db):
    from app import recall
    add_item(db, "karakeep", "bookmark", "a", title="埋め込みモデルの比較",
             text="埋め込みモデルの比較")
    add_item(db, "zotero", "reference", "b", title="週次レビューの研究",
             text="週次レビューの研究")
    out = recall.related(["埋め込みモデル", "週次レビュー"], now=NOW)
    by_id = {r["external_id"]: r for r in out}
    assert by_id["a"]["reason"]["query"] == "埋め込みモデル"
    assert by_id["b"]["reason"]["query"] == "週次レビュー"


def test_related_falls_back_to_keyword_without_embeddings(db, monkeypatch):
    """No embeddings in the archive: _active_embedding_provider raises and
    related() must silently use keyword mode (S4)."""
    from app import recall
    add_item(db, "karakeep", "bookmark", "kw", title="縮退経路の確認",
             text="縮退経路の確認")
    called = {}

    def boom():
        called["yes"] = True
        raise RuntimeError("no embeddings exist")

    monkeypatch.setattr(db, "_active_embedding_provider", boom)
    out = recall.related(["縮退経路"], now=NOW)
    assert called.get("yes")
    assert [r["external_id"] for r in out] == ["kw"]
    assert out[0]["reason"]["match_reason"] == "keyword"


def test_related_includes_old_conversations(db):
    from app import recall
    make_conv(db, "c-old", title="昔のRAG議論", text="RAGアーキテクチャの議論",
              updated=OLD)
    out = recall.related(["RAGアーキテクチャ"], now=NOW)
    assert any(r["kind"] == "conversation" and r["external_id"] == "c-old"
               for r in out)


def test_related_skips_noise_titled_conversations(db):
    """Security-review boilerplate is excluded from activity; it must not
    resurface through related() either (seen in the real-data M4 run)."""
    from app import recall
    make_conv(db, "noise", updated=OLD, text="外部脳の検討メモ",
              title="Review this change for security vulnerabilities. And tell me more")
    make_conv(db, "keep", updated=OLD, text="外部脳の検討メモ",
              title="外部脳の議論")
    out = recall.related(["外部脳"], now=NOW)
    ids = {r["external_id"] for r in out}
    assert "keep" in ids
    assert "noise" not in ids


def test_content_terms_drop_ascii_fragments():
    from app.recall import _content_terms
    assert _content_terms("外部脳の週次レビュー設計") == ["外部脳", "週次レビュー設計"]
    # "on"/"X" too short, stopwords dropped; real tokens survive
    terms = _content_terms("Yuichi Uemura (@u1) on X and the CodexBar")
    assert "on" not in terms and "X" not in terms
    assert "and" not in terms and "the" not in terms
    assert "CodexBar" in terms
    assert _content_terms("T1 to on of") == []


# --- weekly_activity ---------------------------------------------------------

def test_weekly_activity_windows_and_caps(db):
    from app import recall
    for i in range(12):  # 12 in-window bookmarks → capped at 10
        add_item(db, "karakeep", "bookmark", f"bm-{i}", title=f"今週の記事{i}",
                 created=THIS_WEEK, updated=THIS_WEEK, tags=[])
    add_item(db, "karakeep", "bookmark", "bm-old", title="先月の記事",
             created=OLD, updated=OLD, tags=[])
    add_item(db, "zotero", "reference", "ref-1", title="今週の論文",
             created=THIS_WEEK, updated=THIS_WEEK)
    add_item(db, "obsidian", "note", "External Brain/10 Themes/外部脳.md",
             title="外部脳", created=THIS_WEEK, updated=THIS_WEEK,
             folder="10 Themes")
    act = recall.weekly_activity(now=NOW)
    assert len(act["discoveries"]) == 10
    assert all("先月の記事" != d["title"] for d in act["discoveries"])
    assert [r["title"] for r in act["references"]] == ["今週の論文"]
    assert [n["title"] for n in act["notes"]] == ["外部脳"]


def test_weekly_activity_to_review_first(db):
    from app import recall
    add_item(db, "karakeep", "bookmark", "plain", title="普通の記事",
             created="2026-07-03T00:00:00Z", updated="2026-07-03T00:00:00Z",
             tags=[])
    add_item(db, "karakeep", "bookmark", "review", title="要レビュー記事",
             created="2026-07-01T00:00:00Z", updated="2026-07-01T00:00:00Z",
             tags=["to-review"])
    act = recall.weekly_activity(now=NOW)
    # to-review outranks recency
    assert [d["external_id"] for d in act["discoveries"]] == ["review", "plain"]


def test_weekly_activity_conversation_rules(db):
    from app import recall
    make_conv(db, "keep", title="実のある議論", n_messages=5)
    make_conv(db, "short", title="短い会話", n_messages=3)      # <4 messages
    make_conv(db, "noise", title="New chat", n_messages=5)      # noise title
    make_conv(db, "old", title="先月の議論", n_messages=5, updated=OLD)
    act = recall.weekly_activity(now=NOW)
    assert [t["title"] for t in act["thoughts"]] == ["実のある議論"]
    assert act["thoughts"][0]["message_count"] == 5
    assert act["thoughts"][0]["item_id"] is not None


# --- weekly_digest -----------------------------------------------------------

def test_weekly_digest_relates_past_to_this_week(db):
    from app import recall
    make_conv(db, "now", title="外部脳の週次レビュー設計", n_messages=5,
              text="外部脳の週次レビュー設計")
    add_item(db, "karakeep", "bookmark", "past", title="外部脳の記事",
             text="外部脳の記事")  # OLD dates
    digest = recall.weekly_digest(now=NOW)
    assert [t["external_id"] for t in digest["activity"]["thoughts"]] == ["now"]
    rel_ids = {r["external_id"] for r in digest["related"]}
    assert "past" in rel_ids
    for r in digest["related"]:
        assert r["reason"]["query"]  # every related row explains itself


def test_weekly_digest_drops_activity_items_from_related(db):
    from app import recall
    digest = recall.weekly_digest(now=NOW)  # empty archive
    assert digest["related"] == []
    assert digest["activity"]["thoughts"] == []
