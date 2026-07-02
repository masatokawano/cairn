"""Karakeep API connector。

旧 bash + jq 実装は 1 ページ（100 件）しか読まず、to-review が 100 件超で
取りこぼすバグがあった。nextCursor を追従するページネーションで解消する
（上限ページ数は設定値 KARAKEEP_MAX_PAGES）。
"""

from __future__ import annotations

import urllib.parse

from brainsync import httpjson

REVIEW_TAG = "to-review"
DEFAULT_PAGE_LIMIT = 100
DEFAULT_MAX_PAGES = 10


def fetch_bookmarks(
    base_url: str,
    api_key: str,
    *,
    page_limit: int = DEFAULT_PAGE_LIMIT,
    max_pages: int = DEFAULT_MAX_PAGES,
    timeout: float = 30,
    get=httpjson.get_json,
) -> list[dict]:
    """全ブックマークを新しい順で取得する（nextCursor 追従、max_pages で打ち切り）。"""
    headers = {"Authorization": f"Bearer {api_key}"}
    bookmarks: list[dict] = []
    cursor: str | None = None

    for _ in range(max(1, max_pages)):
        query: dict[str, str] = {
            "limit": str(int(page_limit)),
            "sortOrder": "desc",
        }
        if cursor:
            query["cursor"] = cursor
        url = (
            f"{base_url.rstrip('/')}/api/v1/bookmarks?"
            f"{urllib.parse.urlencode(query)}"
        )
        payload, _ = get(url, headers=headers, timeout=timeout)
        bookmarks.extend(payload.get("bookmarks", []))
        cursor = payload.get("nextCursor")
        if not cursor:
            break

    return bookmarks


def bookmark_tags(bookmark: dict) -> list[str]:
    return [
        tag.get("name", "")
        for tag in (bookmark.get("tags") or [])
        if tag.get("name")
    ]


def bookmark_title(bookmark: dict) -> str:
    content = bookmark.get("content") or {}
    return (
        bookmark.get("title")
        or content.get("title")
        or content.get("url")
        or "無題"
    )


def bookmark_url(bookmark: dict) -> str:
    return (bookmark.get("content") or {}).get("url") or ""


def select_to_review(bookmarks: list[dict]) -> list[dict]:
    return [b for b in bookmarks if REVIEW_TAG in bookmark_tags(b)]


def count_untagged(bookmarks: list[dict]) -> int:
    """タグなし（= 未整理の新規）項目の件数。"""
    return sum(1 for b in bookmarks if not bookmark_tags(b))
