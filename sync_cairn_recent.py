#!/usr/bin/env python3

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path


def load_config(path: Path) -> dict[str, str]:
    command = [
        "/bin/bash",
        "-c",
        f"set -a; source {path!s}; env -0",
    ]
    result = subprocess.run(
        command,
        check=True,
        capture_output=True,
    )

    env: dict[str, str] = {}
    for item in result.stdout.split(b"\0"):
        if b"=" in item:
            key, value = item.split(b"=", 1)
            env[key.decode()] = value.decode()
    return env


def parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def markdown_escape(value: str) -> str:
    return value.replace("\n", " ").strip()


def main() -> int:
    project_dir = Path(__file__).resolve().parent
    config_path = project_dir / "config.env"

    if not config_path.exists():
        print("config.env がありません", file=sys.stderr)
        return 1

    config = load_config(config_path)

    cairn_url = config.get("CAIRN_URL")
    vault = config.get("OBSIDIAN_VAULT")
    external_brain_dir = config.get("OBSIDIAN_EXTERNAL_BRAIN_DIR")

    if not cairn_url or not vault or not external_brain_dir:
        print(
            "CAIRN_URL、OBSIDIAN_VAULT、"
            "OBSIDIAN_EXTERNAL_BRAIN_DIRを確認してください",
            file=sys.stderr,
        )
        return 1

    api_url = f"{cairn_url.rstrip('/')}/api/conversations?limit=500"

    request = urllib.request.Request(
        api_url,
        headers={"Accept": "application/json"},
    )

    with urllib.request.urlopen(request, timeout=15) as response:
        payload = json.load(response)

    conversations = payload.get("results", [])
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)

    excluded_title_prefixes = (
        "Review this change for security vulnerabilities.",
        "You are a security expert reviewing",
    )

    excluded_exact_titles = {
        "New chat",
        "User Request: Help Needed",
        "Untitled",
    }

    def is_review_candidate(item: dict) -> bool:
        title = (item.get("title") or "").strip()
        message_count = int(item.get("message_count", 0))

        if not title:
            return False

        if title in excluded_exact_titles:
            return False

        if title.startswith(excluded_title_prefixes):
            return False

        # 単発質問や自動処理ログを週次レビューから除外する。
        if message_count < 4:
            return False

        return True

    recent = [
        item
        for item in conversations
        if parse_timestamp(item["updated_at"]) >= cutoff
        and is_review_candidate(item)
    ]

    recent.sort(
        key=lambda item: parse_timestamp(item["updated_at"]),
        reverse=True,
    )

    target_dir = (
        Path(vault)
        / external_brain_dir
        / "90 Auto"
    )
    target_dir.mkdir(parents=True, exist_ok=True)

    target_file = target_dir / "cairn-recent.md"
    temporary_file = target_file.with_suffix(".md.tmp")

    generated = datetime.now().astimezone()

    lines = [
        "---",
        "source: cairn",
        "type: recent-conversations-index",
        f"generated: {generated:%Y-%m-%d %H:%M:%S%z}",
        "lookback_days: 7",
        f"item_count: {len(recent)}",
        "---",
        "",
        "# Cairn — 直近7日間の会話",
        "",
        "Cairnに保存された最近の生成AI対話の自動一覧です。",
        "",
    ]

    for item in recent:
        title = markdown_escape(item.get("title") or "無題")
        source = item.get("source", "unknown")
        conversation_id = item["id"]
        updated_at = item["updated_at"]
        message_count = item.get("message_count", 0)
        project_dir = (item.get("meta") or {}).get("cwd")

        lines.extend(
            [
                f"## {title}",
                "",
                f"- Source: `{source}`",
                f"- Cairn ID: `{conversation_id}`",
                f"- 更新日時: {updated_at}",
                f"- メッセージ数: {message_count}",
            ]
        )

        if project_dir:
            lines.append(f"- Project: `{project_dir}`")

        lines.extend(
            [
                "",
                "- [ ] 内容を確認",
                "- [ ] Obsidianへ反映",
                "- [ ] 未解決課題を抽出",
                "",
                "---",
                "",
            ]
        )

    temporary_file.write_text(
        "\n".join(lines),
        encoding="utf-8",
    )
    os.replace(temporary_file, target_file)

    print(f"Created: {target_file}")
    print(f"Items: {len(recent)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
