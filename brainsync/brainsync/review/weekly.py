"""週次レビューの合成（create_weekly_review.sh の置換）。

T2 時点では旧実装と同じく `90 Auto/` の markdown から項目を抽出して合成する
（awk は Python に置換済み）。state JSON からの直接合成への移行は T3。
既存週ファイルは上書きしない（責務分界の不変条件 3）。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from brainsync.connectors import obsidian

# 週指定の上書き用環境変数（テスト時に BRAIN_SYNC_WEEK=2099-W01 のように使う）
WEEK_ENV = "BRAIN_SYNC_WEEK"

SOURCE_FILES = {
    "karakeep": "karakeep-to-review.md",
    "cairn": "cairn-recent.md",
    "zotero": "zotero-recent.md",
    "obsidian": "obsidian-context.md",
}

_MISSING_PLACEHOLDER = "_データファイルがまだありません。_"


def current_week(now: datetime | None = None) -> str:
    """ISO 週番号ベースの週ラベル（例: 2026-W27）。"""
    if now is None:
        now = datetime.now().astimezone()
    return f"{now:%G-W%V}"


def extract_items(source_file: Path) -> str:
    """自動一覧から項目部分（最初の `## ` 見出し以降）を抜き出す。

    frontmatter・文書タイトル・説明文は除く。旧 awk 実装と同じ挙動。
    """
    if not source_file.is_file():
        return _MISSING_PLACEHOLDER + "\n"

    lines = source_file.read_text(encoding="utf-8").splitlines()
    for index, line in enumerate(lines):
        if line.startswith("## "):
            return "\n".join(lines[index:]) + "\n"
    return ""


def build_weekly(
    sections: dict[str, str],
    week: str,
    created: datetime,
) -> str:
    """4 ソースの抽出結果から週次レビュー本文を組み立てる。"""
    header = f"""---
type: weekly-external-brain-review
week: {week}
created: {created:%Y-%m-%d %H:%M:%S}
status: open
sources:
  - karakeep
  - cairn
  - zotero
  - obsidian
---

# External Brain Weekly Review — {week}

## 今週の処理方針

- [ ] Karakeepの保存資料を確認する
- [ ] Cairnの重要な対話を確認する
- [ ] Zoteroの新規資料を確認する
- [ ] KarakeepからZoteroへ昇格する資料を選ぶ
- [ ] Obsidianへ反映する着想・結論を選ぶ
- [ ] 未解決課題を整理する
- [ ] レビューを完了する

---

# Karakeep：発見したもの

"""

    footer = """
---

# 今週の統合メモ

## 繰り返し現れたテーマ

## 新しく得た着想

## 根拠資料として残すもの

## 過去の考えから変化した点

## 未解決の問い

## 来週行うこと

"""

    return (
        header
        + sections.get("karakeep", "")
        + "\n---\n\n# Cairn：考えた過程\n\n"
        + sections.get("cairn", "")
        + "\n---\n\n# Zotero：根拠資料\n\n"
        + sections.get("zotero", "")
        + "\n---\n\n# Obsidian：現在の理解\n\n"
        + sections.get("obsidian", "")
        + footer
    )


def create_weekly_review(
    vault: str | Path,
    external_brain_dir: str,
    *,
    week: str | None = None,
    now: datetime | None = None,
) -> Path | None:
    """週次レビューを生成する。既存週なら何も書かず None を返す。"""
    if now is None:
        now = datetime.now().astimezone()
    if week is None:
        week = current_week(now)

    auto = obsidian.auto_dir(vault, external_brain_dir)
    sections = {
        source: extract_items(auto / filename)
        for source, filename in SOURCE_FILES.items()
    }

    content = build_weekly(sections, week, now)
    return obsidian.write_weekly_file(vault, external_brain_dir, week, content)
