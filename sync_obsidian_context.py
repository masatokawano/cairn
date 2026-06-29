#!/usr/bin/env python3

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


LOOKBACK_DAYS = 30


def load_config(path: Path) -> dict[str, str]:
    result = subprocess.run(
        ["/bin/bash", "-c", f"set -a; source {str(path)!r}; env -0"],
        check=True,
        capture_output=True,
    )

    env: dict[str, str] = {}
    for item in result.stdout.split(b"\0"):
        if b"=" in item:
            key, value = item.split(b"=", 1)
            env[key.decode()] = value.decode()
    return env


def note_title(path: Path) -> str:
    return path.stem


def obsidian_link(root: Path, path: Path) -> str:
    relative = path.relative_to(root).with_suffix("")
    return f"[[{relative.as_posix()}]]"


def collect_notes(root: Path, folder: Path, cutoff: datetime) -> list[dict]:
    if not folder.exists():
        return []

    notes = []
    for path in folder.rglob("*.md"):
        if path.name.startswith("."):
            continue

        modified = datetime.fromtimestamp(
            path.stat().st_mtime,
            tz=timezone.utc,
        )

        if modified < cutoff:
            continue

        notes.append(
            {
                "path": path,
                "title": note_title(path),
                "modified": modified,
                "link": obsidian_link(root, path),
            }
        )

    return sorted(notes, key=lambda x: x["modified"], reverse=True)


def main() -> int:
    project_dir = Path(__file__).resolve().parent
    config_path = project_dir / "config.env"

    if not config_path.exists():
        print("config.env がありません", file=sys.stderr)
        return 1

    config = load_config(config_path)

    vault_value = config.get("OBSIDIAN_VAULT")
    external_brain_dir = config.get("OBSIDIAN_EXTERNAL_BRAIN_DIR")

    if not vault_value or not external_brain_dir:
        print(
            "OBSIDIAN_VAULTまたはOBSIDIAN_EXTERNAL_BRAIN_DIRが未設定です",
            file=sys.stderr,
        )
        return 1

    vault = Path(vault_value)
    external_brain = vault / external_brain_dir

    themes_dir = external_brain / "10 Themes"
    projects_dir = external_brain / "20 Projects"
    target_dir = external_brain / "90 Auto"
    target_dir.mkdir(parents=True, exist_ok=True)

    cutoff = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)

    themes = collect_notes(vault, themes_dir, cutoff)
    projects = collect_notes(vault, projects_dir, cutoff)

    target_file = target_dir / "obsidian-context.md"
    temp_file = target_file.with_suffix(".md.tmp")

    generated = datetime.now().astimezone()

    lines = [
        "---",
        "source: obsidian",
        "type: current-context-index",
        f"generated: {generated:%Y-%m-%d %H:%M:%S%z}",
        f"lookback_days: {LOOKBACK_DAYS}",
        f"theme_count: {len(themes)}",
        f"project_count: {len(projects)}",
        "---",
        "",
        "# Obsidian — 現在の理解",
        "",
        "最近更新されたテーマノートとプロジェクトノートの自動一覧です。",
        "",
        "## Themes",
        "",
    ]

    if themes:
        for item in themes:
            local_time = item["modified"].astimezone()
            lines.append(
                f"- {item['link']} — 更新 {local_time:%Y-%m-%d %H:%M}"
            )
    else:
        lines.append("_最近更新されたテーマノートはありません。_")

    lines.extend(
        [
            "",
            "## Projects",
            "",
        ]
    )

    if projects:
        for item in projects:
            local_time = item["modified"].astimezone()
            lines.append(
                f"- {item['link']} — 更新 {local_time:%Y-%m-%d %H:%M}"
            )
    else:
        lines.append("_最近更新されたプロジェクトノートはありません。_")

    lines.extend(
        [
            "",
            "## Review",
            "",
            "- [ ] 現在のテーマを確認",
            "- [ ] 進行中プロジェクトを確認",
            "- [ ] 今週の資料・対話との関連を確認",
            "",
        ]
    )

    temp_file.write_text("\n".join(lines), encoding="utf-8")
    os.replace(temp_file, target_file)

    print(f"Created: {target_file}")
    print(f"Themes: {len(themes)}")
    print(f"Projects: {len(projects)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
