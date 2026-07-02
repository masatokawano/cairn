from __future__ import annotations

from datetime import datetime, timedelta, timezone

from brainsync.render import auto

JST = timezone(timedelta(hours=9))
GENERATED = datetime(2026, 7, 2, 12, 34, 56, tzinfo=JST)


def test_render_cairn_recent_structure(load_fixture):
    items = load_fixture("cairn_conversations.json")["results"]
    content = auto.render_cairn_recent([items[0], items[6]], GENERATED)
    lines = content.splitlines()

    assert lines[0] == "---"
    assert "source: cairn" in lines
    assert "type: recent-conversations-index" in lines
    assert "generated: 2026-07-02 12:34:56+0900" in lines
    assert "lookback_days: 7" in lines
    assert "item_count: 2" in lines
    assert "# Cairn — 直近7日間の会話" in lines

    assert "## Cairn T2 実装" in lines
    assert "- Source: `claude_cli`" in lines
    assert "- Cairn ID: `1`" in lines
    assert "- メッセージ数: 12" in lines
    assert "- Project: `/home/user/project`" in lines
    assert "- [ ] 内容を確認" in lines

    # 外部由来タイトルの無害化: 見出し化・wikilink・code span 脱出を防ぐ
    assert "## \\# evil \\[title\\] with 'code' and \\| pipe" in lines


def test_render_karakeep_review_structure(load_fixture):
    bookmarks = (
        load_fixture("karakeep_page1.json")["bookmarks"]
        + load_fixture("karakeep_page2.json")["bookmarks"]
    )
    content = auto.render_karakeep_review(bookmarks, GENERATED)
    lines = content.splitlines()

    # 旧 bash 実装との互換: generated はタイムゾーンなし
    assert "generated: 2026-07-02 12:34:56" in lines
    assert "item_count: 5" in lines
    assert "# Karakeep — 要レビュー" in lines

    assert "## LLM agents survey" in lines
    assert "- URL: https://example.com/llm-agents" in lines
    assert "- Karakeep ID: `bk_001`" in lines
    assert "- タグ: to-review, ai" in lines
    assert "- [ ] Zoteroへ昇格" in lines

    # URL なしの項目には URL 行が出ない（# は行頭のみエスケープ対象）
    evil_index = lines.index("## Evil # \\[x\\] 'rm -rf' \\| pipe title")
    assert lines[evil_index + 2] == "- Karakeep ID: `bk_102`"


def test_render_zotero_recent_structure(load_fixture):
    items = load_fixture("zotero_items.json")
    content = auto.render_zotero_recent(items[:2], GENERATED)
    lines = content.splitlines()

    assert "source: zotero" in lines
    assert "generated: 2026-07-02 12:34:56+0900" in lines
    assert "item_count: 2" in lines

    assert "## Attention Is All You Need" in lines
    assert "- 種別: `journalArticle`" in lines
    assert "- Zotero Key: `KEY1`" in lines
    assert "- 著者: Ashish Vaswani, Google Brain" in lines
    assert "- DOI: `10.1000/xyz`" in lines
    assert "- URL: https://example.com/paper" in lines
    assert "- タグ: transformers, attention" in lines

    # 空タイトルは 無題、任意フィールドの行は出ない
    assert "## 無題" in lines
    assert sum(1 for line in lines if line.startswith("- DOI:")) == 1


def test_render_obsidian_context_placeholders():
    content = auto.render_obsidian_context([], [], GENERATED)
    lines = content.splitlines()

    assert "theme_count: 0" in lines
    assert "project_count: 0" in lines
    assert "_最近更新されたテーマノートはありません。_" in lines
    assert "_最近更新されたプロジェクトノートはありません。_" in lines


def test_render_obsidian_context_lists_notes():
    modified = datetime(2026, 7, 1, 9, 0, 0, tzinfo=JST)
    themes = [
        {
            "title": "外部脳",
            "link": "[[External Brain/10 Themes/外部脳]]",
            "modified": modified,
        }
    ]
    content = auto.render_obsidian_context(themes, [], GENERATED)

    # 表示はマシンのローカルタイムゾーンに変換される（旧実装と同じ）
    expected_time = f"{modified.astimezone():%Y-%m-%d %H:%M}"
    assert (
        f"- [[External Brain/10 Themes/外部脳]] — 更新 {expected_time}" in content
    )
