"""brainsync CLI — launchd と手動実行の唯一の入口。

    python -m brainsync sync-cairn | sync-karakeep | sync-zotero | sync-obsidian
    python -m brainsync weekly
    python -m brainsync check cairn | karakeep | zotero | obsidian

旧 run_*.sh / check_*.sh / sync_*.{sh,py} / create_weekly_review.sh を置換する。
パスはハードコードせず、config.env（パッケージ位置から解決、
BRAIN_SYNC_CONFIG で上書き可）から読む。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path

from brainsync import secrets
from brainsync.config import ConfigError, load_config, require as _require
from brainsync.connectors import cairn_api, karakeep, obsidian, zotero
from brainsync.render import auto as render_auto
from brainsync.review import weekly
from brainsync.secrets import SecretError


def _karakeep_max_pages(config: dict[str, str]) -> int:
    raw = config.get("KARAKEEP_MAX_PAGES", "")
    if not raw:
        return karakeep.DEFAULT_MAX_PAGES
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"KARAKEEP_MAX_PAGES が整数ではありません: {raw!r}")
    if value < 1:
        raise ConfigError("KARAKEEP_MAX_PAGES は 1 以上にしてください")
    return value


def cmd_sync_cairn(config: dict[str, str]) -> int:
    cairn_url, vault, brain_dir = _require(
        config, "CAIRN_URL", "OBSIDIAN_VAULT", "OBSIDIAN_EXTERNAL_BRAIN_DIR"
    )

    if not cairn_api.is_available(cairn_url):
        print("Cairn API is unavailable; keeping the previous Obsidian file.")
        return 0

    conversations = cairn_api.fetch_conversations(cairn_url)
    recent = cairn_api.select_recent_reviews(conversations)

    content = render_auto.render_cairn_recent(recent, datetime.now().astimezone())
    target = obsidian.write_auto_file(vault, brain_dir, "cairn-recent.md", content)

    print(f"Created: {target}")
    print(f"Items: {len(recent)}")
    return 0


def cmd_sync_karakeep(config: dict[str, str]) -> int:
    base_url, vault, brain_dir = _require(
        config, "KARAKEEP_URL", "OBSIDIAN_VAULT", "OBSIDIAN_EXTERNAL_BRAIN_DIR"
    )
    api_key = secrets.get_secret(secrets.KARAKEEP_SERVICE)

    bookmarks = karakeep.fetch_bookmarks(
        base_url, api_key, max_pages=_karakeep_max_pages(config)
    )
    to_review = karakeep.select_to_review(bookmarks)
    untagged = karakeep.count_untagged(bookmarks)

    content = render_auto.render_karakeep_review(
        to_review, datetime.now().astimezone()
    )
    target = obsidian.write_auto_file(
        vault, brain_dir, "karakeep-to-review.md", content
    )

    print(f"Created: {target}")
    print(f"Items: {len(to_review)}")
    print(f"Untagged: {untagged}")
    return 0


def cmd_sync_zotero(config: dict[str, str]) -> int:
    api_url, user_id, vault, brain_dir = _require(
        config,
        "ZOTERO_API_URL",
        "ZOTERO_USER_ID",
        "OBSIDIAN_VAULT",
        "OBSIDIAN_EXTERNAL_BRAIN_DIR",
    )
    api_key = secrets.get_secret(secrets.ZOTERO_SERVICE)

    items, _version = zotero.fetch_recent_items(api_url, user_id, api_key)
    recent = zotero.select_recent(items)

    content = render_auto.render_zotero_recent(recent, datetime.now().astimezone())
    target = obsidian.write_auto_file(vault, brain_dir, "zotero-recent.md", content)

    print(f"Created: {target}")
    print(f"Items: {len(recent)}")
    return 0


def cmd_sync_obsidian(config: dict[str, str]) -> int:
    vault, brain_dir = _require(
        config, "OBSIDIAN_VAULT", "OBSIDIAN_EXTERNAL_BRAIN_DIR"
    )

    root = obsidian.external_brain_root(vault, brain_dir)
    cutoff = datetime.now(timezone.utc) - timedelta(
        days=render_auto.OBSIDIAN_LOOKBACK_DAYS
    )
    themes = obsidian.collect_notes(
        Path(vault), root / obsidian.THEMES_DIR, cutoff
    )
    projects = obsidian.collect_notes(
        Path(vault), root / obsidian.PROJECTS_DIR, cutoff
    )

    content = render_auto.render_obsidian_context(
        themes, projects, datetime.now().astimezone()
    )
    target = obsidian.write_auto_file(
        vault, brain_dir, "obsidian-context.md", content
    )

    print(f"Created: {target}")
    print(f"Themes: {len(themes)}")
    print(f"Projects: {len(projects)}")
    return 0


def cmd_weekly(config: dict[str, str]) -> int:
    vault, brain_dir = _require(
        config, "OBSIDIAN_VAULT", "OBSIDIAN_EXTERNAL_BRAIN_DIR"
    )

    week = os.environ.get(weekly.WEEK_ENV) or None
    target = weekly.create_weekly_review(vault, brain_dir, week=week)
    if target is None:
        resolved_week = week or weekly.current_week()
        existing = obsidian.weekly_dir(vault, brain_dir) / f"{resolved_week}.md"
        print(f"Weekly review already exists: {existing}")
        return 0

    print(f"Created: {target}")
    return 0


def cmd_check_cairn(config: dict[str, str]) -> int:
    (cairn_url,) = _require(config, "CAIRN_URL")
    results = cairn_api.check(cairn_url)

    print("Cairn API connection OK")
    print(f"Retrieved conversations: {len(results)}")
    for item in results:
        print(
            f"- [{item['source']}] #{item['id']} {item['title']}"
            f" — {item['message_count']} messages, updated {item['updated_at']}"
        )
    return 0


def cmd_check_karakeep(config: dict[str, str]) -> int:
    (base_url,) = _require(config, "KARAKEEP_URL")
    api_key = secrets.get_secret(secrets.KARAKEEP_SERVICE)

    bookmarks = karakeep.fetch_bookmarks(
        base_url, api_key, max_pages=_karakeep_max_pages(config)
    )
    to_review = [
        {
            "id": item.get("id"),
            "title": item.get("title"),
            "url": karakeep.bookmark_url(item) or None,
            "tags": karakeep.bookmark_tags(item),
            "createdAt": item.get("createdAt"),
        }
        for item in karakeep.select_to_review(bookmarks)
    ]
    print(json.dumps(to_review, ensure_ascii=False, indent=2))
    return 0


def cmd_check_zotero(config: dict[str, str]) -> int:
    api_url, user_id = _require(config, "ZOTERO_API_URL", "ZOTERO_USER_ID")
    api_key = secrets.get_secret(secrets.ZOTERO_SERVICE)

    items = zotero.check(api_url, user_id, api_key)

    print("Zotero API connection OK")
    print(f"Retrieved items: {len(items)}")
    for item in items:
        data = item.get("data", {})
        title = data.get("title") or "無題"
        print(
            f"- [{data.get('itemType')}] {title}"
            f" — key={item.get('key')}, modified={data.get('dateModified')}"
        )
    return 0


def cmd_check_obsidian(config: dict[str, str]) -> int:
    vault, brain_dir = _require(
        config, "OBSIDIAN_VAULT", "OBSIDIAN_EXTERNAL_BRAIN_DIR"
    )

    target_dir = obsidian.auto_dir(vault, brain_dir)
    if not target_dir.is_dir():
        print(f"対象ディレクトリが見つかりません: {target_dir}", file=sys.stderr)
        return 1

    content = render_auto.render_connection_test(datetime.now().astimezone())
    target = obsidian.write_auto_file(vault, brain_dir, "brain-sync-test.md", content)

    print(f"Created: {target}")
    return 0


_CHECK_COMMANDS = {
    "cairn": cmd_check_cairn,
    "karakeep": cmd_check_karakeep,
    "zotero": cmd_check_zotero,
    "obsidian": cmd_check_obsidian,
}

_SYNC_COMMANDS = {
    "sync-cairn": cmd_sync_cairn,
    "sync-karakeep": cmd_sync_karakeep,
    "sync-zotero": cmd_sync_zotero,
    "sync-obsidian": cmd_sync_obsidian,
    "weekly": cmd_weekly,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="brainsync",
        description="Karakeep / Cairn / Zotero / Obsidian の統合層",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="config.env のパス（既定: brainsync/config.env）",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    for name in _SYNC_COMMANDS:
        subparsers.add_parser(name)
    check_parser = subparsers.add_parser("check")
    check_parser.add_argument("target", choices=sorted(_CHECK_COMMANDS))

    args = parser.parse_args(argv)

    try:
        config = load_config(args.config)
        if args.command == "check":
            return _CHECK_COMMANDS[args.target](config)
        return _SYNC_COMMANDS[args.command](config)
    except (ConfigError, SecretError, cairn_api.CheckError, zotero.CheckError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except urllib.error.URLError as exc:
        print(f"API へ接続できませんでした: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
