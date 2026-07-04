"""deliver/auto_lists.py — 90 Auto list formats (M3, DESIGN.md §5.5).

Pure content generation: legacy-format frontmatter, exclusion rules from the
old sync_cairn_recent.py, lookback windows, and the §6.1 rule that untrusted
titles cannot break the markdown structure.
"""
import importlib
from datetime import datetime, timezone

import pytest

NOW = datetime(2026, 7, 4, 12, 0, 0, tzinfo=timezone.utc)


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


def add_item(db, source, kind, external_id, *, title, url=None, doi=None,
             created="2026-07-03T00:00:00Z", updated="2026-07-03T00:00:00Z", **meta):
    db.upsert_items(source, kind, [{
        "external_id": external_id, "title": title, "url": url, "doi": doi,
        "created_at": created, "updated_at": updated, "meta": meta,
    }])


def make_conv(db, source_id, *, title, n_messages=4, updated="2026-07-03T00:00:00Z", cwd=None):
    from app.parsers.base import ParsedConversation, ParsedMessage
    meta = {"cwd": cwd} if cwd else {}
    db.upsert_conversations([ParsedConversation(
        source="chatgpt", source_id=source_id, title=title,
        messages=[ParsedMessage(role="user", text=f"m{i}", created_at=updated)
                  for i in range(n_messages)],
        created_at=updated, updated_at=updated, meta=meta,
    )])


def test_karakeep_list_filters_to_review_tag(db):
    add_item(db, "karakeep", "bookmark", "bm-1", title="読むべき記事",
             url="https://example.com/a", tags=["to-review", "ai"])
    add_item(db, "karakeep", "bookmark", "bm-2", title="ただの記事",
             url="https://example.com/b", tags=["ai"])
    from app.deliver import auto_lists
    md = auto_lists.karakeep_to_review(NOW)
    assert "item_count: 1" in md
    assert "## 読むべき記事" in md
    assert "ただの記事" not in md
    assert "source: karakeep" in md and "type: review-index" in md
    assert "- [ ] Zoteroへ昇格" in md


def test_cairn_recent_exclusion_rules(db):
    make_conv(db, "c1", title="実のある議論", n_messages=5, cwd="/tmp/proj")
    make_conv(db, "c2", title="New chat", n_messages=5)          # exact excluded
    make_conv(db, "c3", title="短い", n_messages=3)               # <4 messages
    make_conv(db, "c4", title="You are a security expert reviewing this",
              n_messages=6)                                       # prefix excluded
    make_conv(db, "c5", title="先週の話", n_messages=5,
              updated="2026-06-20T00:00:00Z")                     # outside 7d
    from app.deliver import auto_lists
    md = auto_lists.cairn_recent(NOW)
    assert "item_count: 1" in md
    assert "## 実のある議論" in md
    assert "- Project: `/tmp/proj`" in md
    assert "New chat" not in md and "短い" not in md
    assert "security expert" not in md and "先週の話" not in md


def test_zotero_recent_lookback_and_fields(db):
    add_item(db, "zotero", "reference", "KEY1", title="新しい論文",
             updated="2026-07-02T00:00:00Z", doi="10.1000/xyz",
             itemType="journalArticle", creators=["Ada Lovelace"], tags=["ml"])
    add_item(db, "zotero", "reference", "KEY2", title="古い論文",
             updated="2026-06-01T00:00:00Z")
    from app.deliver import auto_lists
    md = auto_lists.zotero_recent(NOW)
    assert "item_count: 1" in md
    assert "## 新しい論文" in md and "古い論文" not in md
    assert "- 著者: Ada Lovelace" in md
    assert "- DOI: `10.1000/xyz`" in md
    assert "- 種別: `journalArticle`" in md


def test_obsidian_context_splits_themes_projects(db):
    add_item(db, "obsidian", "note", "External Brain/10 Themes/外部脳.md",
             title="外部脳", updated="2026-07-01T00:00:00Z",
             folder="10 Themes", text="本文")
    add_item(db, "obsidian", "note", "External Brain/20 Projects/cairn.md",
             title="cairn", updated="2026-07-01T00:00:00Z",
             folder="20 Projects", text="本文")
    add_item(db, "obsidian", "note", "External Brain/50 Decisions/D9.md",
             title="D9", updated="2026-07-01T00:00:00Z",
             folder="50 Decisions", text="本文")  # not in themes/projects
    add_item(db, "obsidian", "note", "External Brain/10 Themes/古い.md",
             title="古い", updated="2026-05-01T00:00:00Z",
             folder="10 Themes", text="本文")  # outside 30d
    from app.deliver import auto_lists
    md = auto_lists.obsidian_context(NOW)
    assert "theme_count: 1" in md and "project_count: 1" in md
    assert "[[External Brain/10 Themes/外部脳]]" in md  # .md stripped wikilink
    assert "[[External Brain/20 Projects/cairn]]" in md
    assert "D9" not in md and "古い" not in md


def test_untrusted_title_cannot_break_structure(db):
    evil = "改行注入\n---\nsource: attacker\n# 偽見出し"
    add_item(db, "karakeep", "bookmark", "bm-evil", title=evil,
             url="https://example.com/e", tags=["to-review"])
    from app.deliver import auto_lists
    md = auto_lists.karakeep_to_review(NOW)
    # collapsed to one line: no injected frontmatter or heading lines
    assert "\nsource: attacker" not in md
    assert "\n# 偽見出し" not in md
    assert "## 改行注入 --- source: attacker # 偽見出し" in md


def test_generate_all_returns_four_lists(db):
    from app.deliver import auto_lists
    out = auto_lists.generate_all(NOW)
    assert set(out) == {
        "karakeep-to-review.md", "cairn-recent.md",
        "zotero-recent.md", "obsidian-context.md",
    }
    for name, content in out.items():
        assert content.startswith("---\n"), name
        assert "generated: 2026-07-04" in content, name


def test_empty_db_produces_valid_lists(db):
    from app.deliver import auto_lists
    out = auto_lists.generate_all(NOW)
    assert "item_count: 0" in out["karakeep-to-review.md"]
    assert "item_count: 0" in out["cairn-recent.md"]
    assert "最近更新されたテーマノートはありません" in out["obsidian-context.md"]
