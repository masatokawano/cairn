"""Obsidian Vault connector。

読み取りは Vault 全域（テーマ・プロジェクトの一覧化）、書き込みは
`90 Auto` と `40 Reviews/Weekly` のみ（責務分界の不変条件 3）。
Vault への書き込みは必ずこのモジュールを通す。
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

AUTO_DIR = "90 Auto"
WEEKLY_DIR = "40 Reviews/Weekly"
THEMES_DIR = "10 Themes"
PROJECTS_DIR = "20 Projects"


def external_brain_root(vault: str | Path, external_brain_dir: str) -> Path:
    return Path(vault) / external_brain_dir


def auto_dir(vault: str | Path, external_brain_dir: str) -> Path:
    return external_brain_root(vault, external_brain_dir) / AUTO_DIR


def weekly_dir(vault: str | Path, external_brain_dir: str) -> Path:
    return external_brain_root(vault, external_brain_dir) / WEEKLY_DIR


def write_atomic(target: Path, content: str) -> None:
    """tmp へ書いて os.replace。途中で死んでも半端なファイルが残らない。"""
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, target)


def write_auto_file(
    vault: str | Path,
    external_brain_dir: str,
    filename: str,
    content: str,
) -> Path:
    """`90 Auto/` へ自動一覧を書き込む（機械上書き領域）。"""
    target_dir = auto_dir(vault, external_brain_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / filename
    write_atomic(target, content)
    return target


def write_weekly_file(
    vault: str | Path,
    external_brain_dir: str,
    week: str,
    content: str,
) -> Path | None:
    """`40 Reviews/Weekly/<week>.md` を作成する。既存週は上書きしない。"""
    target_dir = weekly_dir(vault, external_brain_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{week}.md"
    if target.exists():
        return None
    write_atomic(target, content)
    return target


def collect_notes(root: Path, folder: Path, cutoff: datetime) -> list[dict]:
    """folder 以下の markdown ノートを更新降順で列挙する（cutoff より新しいもの）。"""
    if not folder.exists():
        return []

    notes = []
    for path in folder.rglob("*.md"):
        if path.name.startswith("."):
            continue

        modified = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        if modified < cutoff:
            continue

        relative = path.relative_to(root).with_suffix("")
        notes.append(
            {
                "path": path,
                "title": path.stem,
                "modified": modified,
                "link": f"[[{relative.as_posix()}]]",
            }
        )

    return sorted(notes, key=lambda note: note["modified"], reverse=True)
