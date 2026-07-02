#!/usr/bin/env python3

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path


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


def get_keychain_secret(service: str) -> str:
    result = subprocess.run(
        [
            "security",
            "find-generic-password",
            "-a",
            os.environ["USER"],
            "-s",
            service,
            "-w",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def clean_text(value: str | None) -> str:
    return (value or "").replace("\n", " ").strip()


def creator_name(creator: dict) -> str:
    if creator.get("name"):
        return clean_text(creator["name"])

    first = clean_text(creator.get("firstName"))
    last = clean_text(creator.get("lastName"))
    return " ".join(part for part in (first, last) if part)


def main() -> int:
    project_dir = Path(__file__).resolve().parent
    config_path = project_dir / "config.env"

    if not config_path.exists():
        print("config.env がありません", file=sys.stderr)
        return 1

    config = load_config(config_path)

    api_url = config.get("ZOTERO_API_URL")
    user_id = config.get("ZOTERO_USER_ID")
    vault = config.get("OBSIDIAN_VAULT")
    external_brain_dir = config.get("OBSIDIAN_EXTERNAL_BRAIN_DIR")

    if not all((api_url, user_id, vault, external_brain_dir)):
        print(
            "ZOTERO_API_URL、ZOTERO_USER_ID、OBSIDIAN_VAULT、"
            "OBSIDIAN_EXTERNAL_BRAIN_DIRを確認してください",
            file=sys.stderr,
        )
        return 1

    api_key = get_keychain_secret("brain-sync-zotero")

    query = urllib.parse.urlencode(
        {
            "limit": 100,
            "sort": "dateModified",
            "direction": "desc",
        }
    )
    url = f"{api_url.rstrip('/')}/users/{user_id}/items/top?{query}"

    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "Zotero-API-Key": api_key,
            "Zotero-API-Version": "3",
        },
    )

    with urllib.request.urlopen(request, timeout=30) as response:
        items = json.load(response)

    cutoff = datetime.now(timezone.utc) - timedelta(days=7)

    recent = [
        item
        for item in items
        if parse_timestamp(item["data"]["dateModified"]) >= cutoff
    ]

    recent.sort(
        key=lambda item: parse_timestamp(item["data"]["dateModified"]),
        reverse=True,
    )

    target_dir = Path(vault) / external_brain_dir / "90 Auto"
    target_dir.mkdir(parents=True, exist_ok=True)

    target_file = target_dir / "zotero-recent.md"
    temp_file = target_file.with_suffix(".md.tmp")

    generated = datetime.now().astimezone()

    lines = [
        "---",
        "source: zotero",
        "type: recent-items-index",
        f"generated: {generated:%Y-%m-%d %H:%M:%S%z}",
        "lookback_days: 7",
        f"item_count: {len(recent)}",
        "---",
        "",
        "# Zotero — 直近7日間の資料",
        "",
        "Zoteroで最近追加または更新された資料の自動一覧です。",
        "",
    ]

    for item in recent:
        data = item["data"]
        key = item["key"]

        title = clean_text(data.get("title")) or "無題"
        item_type = clean_text(data.get("itemType")) or "unknown"
        modified = clean_text(data.get("dateModified"))
        url_value = clean_text(data.get("url"))
        doi = clean_text(data.get("DOI"))

        creators = [
            creator_name(creator)
            for creator in data.get("creators", [])
            if creator_name(creator)
        ]
        creator_text = ", ".join(creators[:5])

        tags = [
            clean_text(tag.get("tag"))
            for tag in data.get("tags", [])
            if clean_text(tag.get("tag"))
        ]

        lines.extend(
            [
                f"## {title}",
                "",
                f"- 種別: `{item_type}`",
                f"- Zotero Key: `{key}`",
                f"- 更新日時: {modified}",
            ]
        )

        if creator_text:
            lines.append(f"- 著者: {creator_text}")

        if doi:
            lines.append(f"- DOI: `{doi}`")

        if url_value:
            lines.append(f"- URL: {url_value}")

        if tags:
            lines.append(f"- タグ: {', '.join(tags)}")

        lines.extend(
            [
                "",
                "- [ ] 内容を確認",
                "- [ ] Obsidianのテーマへ反映",
                "- [ ] Cairnの関連対話を探す",
                "",
                "---",
                "",
            ]
        )

    temp_file.write_text("\n".join(lines), encoding="utf-8")
    os.replace(temp_file, target_file)

    print(f"Created: {target_file}")
    print(f"Items: {len(recent)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
