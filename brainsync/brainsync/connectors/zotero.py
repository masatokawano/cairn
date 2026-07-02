"""Zotero Web API connector（read-only）。

`?since=<library version>` を使った増分取得は state 層（T3）でカーソルを
持てるようになってから。現状は旧実装と同じ「dateModified 降順で 100 件」。
Last-Modified-Version ヘッダは将来の since 用に返しておく。
"""

from __future__ import annotations

import urllib.parse
from datetime import datetime, timedelta, timezone

from brainsync import httpjson

LOOKBACK_DAYS = 7


def fetch_recent_items(
    api_url: str,
    user_id: str,
    api_key: str,
    *,
    limit: int = 100,
    timeout: float = 30,
    get=httpjson.get_json,
) -> tuple[list[dict], str | None]:
    """トップレベルアイテムを更新順で取得し、(items, library version) を返す。"""
    query = urllib.parse.urlencode(
        {
            "limit": int(limit),
            "sort": "dateModified",
            "direction": "desc",
        }
    )
    url = f"{api_url.rstrip('/')}/users/{user_id}/items/top?{query}"
    items, headers = get(
        url,
        headers={
            "Zotero-API-Key": api_key,
            "Zotero-API-Version": "3",
        },
        timeout=timeout,
    )
    return items, headers.get("Last-Modified-Version")


def select_recent(
    items: list[dict],
    *,
    now: datetime | None = None,
    lookback_days: int = LOOKBACK_DAYS,
) -> list[dict]:
    if now is None:
        now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=lookback_days)

    recent = [
        item
        for item in items
        if httpjson.parse_timestamp(item["data"]["dateModified"]) >= cutoff
    ]
    recent.sort(
        key=lambda item: httpjson.parse_timestamp(item["data"]["dateModified"]),
        reverse=True,
    )
    return recent


def creator_name(creator: dict) -> str:
    if creator.get("name"):
        return " ".join(creator["name"].split())

    first = " ".join((creator.get("firstName") or "").split())
    last = " ".join((creator.get("lastName") or "").split())
    return " ".join(part for part in (first, last) if part)


class CheckError(Exception):
    """接続確認でレスポンス形状が契約に合わなかった。"""


def check(
    api_url: str,
    user_id: str,
    api_key: str,
    *,
    get=httpjson.get_json,
) -> list[dict]:
    """接続確認: 3 件取得してレスポンス形状を検証し、結果を返す。"""
    items, _ = fetch_recent_items(api_url, user_id, api_key, limit=3, get=get)
    if not isinstance(items, list):
        raise CheckError("レスポンスが配列ではありません")
    for item in items:
        if not (
            isinstance(item.get("key"), str)
            and isinstance(item.get("version"), int)
            and isinstance(item.get("data"), dict)
        ):
            raise CheckError(f"item の形が契約に合いません: {item.get('key')!r}")
    return items
