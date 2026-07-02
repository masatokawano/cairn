from __future__ import annotations

from datetime import datetime, timedelta, timezone

from brainsync.review import weekly

JST = timezone(timedelta(hours=9))
NOW = datetime(2026, 7, 2, 12, 34, 56, tzinfo=JST)

BRAIN_DIR = "External Brain"


def make_auto_file(vault, name: str, items_body: str) -> None:
    auto = vault / BRAIN_DIR / "90 Auto"
    auto.mkdir(parents=True, exist_ok=True)
    (auto / name).write_text(
        "\n".join(
            [
                "---",
                "source: test",
                "generated: 2026-07-02 09:00:00",
                "---",
                "",
                "# 文書タイトル",
                "",
                "説明文の段落です。",
                "",
                items_body,
                "",
            ]
        ),
        encoding="utf-8",
    )


def test_extract_items_skips_frontmatter_and_preamble(tmp_path):
    make_auto_file(tmp_path, "cairn-recent.md", "## 項目1\n\n- 詳細\n\n---")
    extracted = weekly.extract_items(
        tmp_path / BRAIN_DIR / "90 Auto" / "cairn-recent.md"
    )
    assert extracted.startswith("## 項目1\n")
    assert "文書タイトル" not in extracted
    assert "説明文" not in extracted
    assert "generated:" not in extracted


def test_extract_items_missing_file(tmp_path):
    extracted = weekly.extract_items(tmp_path / "nope.md")
    assert extracted == "_データファイルがまだありません。_\n"


def test_create_weekly_review_composes_sections(tmp_path):
    make_auto_file(tmp_path, "karakeep-to-review.md", "## K項目\n\n---")
    make_auto_file(tmp_path, "cairn-recent.md", "## C項目\n\n---")
    # zotero-recent.md はあえて作らない → プレースホルダ
    make_auto_file(tmp_path, "obsidian-context.md", "## Themes抜粋")

    target = weekly.create_weekly_review(
        tmp_path, BRAIN_DIR, week="2099-W01", now=NOW
    )

    assert target == tmp_path / BRAIN_DIR / "40 Reviews/Weekly" / "2099-W01.md"
    content = target.read_text(encoding="utf-8")

    assert "week: 2099-W01" in content
    assert "created: 2026-07-02 12:34:56" in content
    assert "# External Brain Weekly Review — 2099-W01" in content
    assert "# Karakeep：発見したもの" in content
    assert "## K項目" in content
    assert "# Cairn：考えた過程" in content
    assert "## C項目" in content
    assert "# Zotero：根拠資料" in content
    assert "_データファイルがまだありません。_" in content
    assert "# Obsidian：現在の理解" in content
    assert "## Themes抜粋" in content
    assert "# 今週の統合メモ" in content
    assert "## 来週行うこと" in content

    # セクションの順序が固定されている
    assert (
        content.index("# Karakeep：発見したもの")
        < content.index("# Cairn：考えた過程")
        < content.index("# Zotero：根拠資料")
        < content.index("# Obsidian：現在の理解")
        < content.index("# 今週の統合メモ")
    )


def test_create_weekly_review_never_overwrites(tmp_path):
    make_auto_file(tmp_path, "cairn-recent.md", "## 初回")
    first = weekly.create_weekly_review(tmp_path, BRAIN_DIR, week="2099-W02", now=NOW)
    assert first is not None
    original = first.read_text(encoding="utf-8")

    make_auto_file(tmp_path, "cairn-recent.md", "## 二回目")
    second = weekly.create_weekly_review(tmp_path, BRAIN_DIR, week="2099-W02", now=NOW)
    assert second is None
    assert first.read_text(encoding="utf-8") == original


def test_current_week_is_iso_week():
    assert weekly.current_week(datetime(2026, 1, 1, tzinfo=JST)) == "2026-W01"
    # ISO 週: 2027-01-01 は金曜 → 2026-W53
    assert weekly.current_week(datetime(2027, 1, 1, tzinfo=JST)) == "2026-W53"
