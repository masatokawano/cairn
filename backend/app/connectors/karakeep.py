"""Read-only Karakeep connector (M1, DESIGN.md §5.1).

Bookmarks → items(kind='bookmark', source='karakeep'). Tags, description,
notes and (capped) full text go into meta — that is the M2 indexing text
(§4: タイトル + 説明/メモ + タグ、原本複製はしない).

Config: ``CAIRN_KARAKEEP_URL`` (base URL, e.g. https://karakeep.example.com).
Auth: Keychain service ``brain-sync-karakeep`` (D8), account = login user.

Incremental model: the v1 API has no modified-since filter, so we fetch
newest-first (createdAt desc, cursor pagination) and stop as soon as a whole
page is at-or-older than the stored ``last_created_at`` — that boundary page
is still upserted, so same-timestamp arrivals are not lost, and content_hash
comparison turns the overlap into skips. Consequence: edits to bookmarks
older than the cursor (tag changes etc.) are only picked up by
``cairn sync karakeep --full``, which sweeps every page.
"""
from __future__ import annotations

import os

import httpx

from .. import db
from ..core import keychain, urlnorm
from . import ConnectorError

SERVICE = "brain-sync-karakeep"
SOURCE = "karakeep"
PAGE_LIMIT = 100
TIMEOUT = 30.0
# Cap on free-text fields kept in meta (excerpt, not a copy of the original).
TEXT_CAP = 10_000


class KarakeepClient:
    """Minimal read-only client. GET /api/v1/bookmarks only."""

    def __init__(self, base_url: str, api_key: str, client: httpx.Client | None = None):
        self._base = base_url.rstrip("/")
        self._client = client or httpx.Client(timeout=TIMEOUT)
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
        }

    def page(self, cursor: str | None = None) -> dict:
        params: dict = {"limit": PAGE_LIMIT, "sortOrder": "desc"}
        if cursor:
            params["cursor"] = cursor
        resp = self._client.get(
            f"{self._base}/api/v1/bookmarks", params=params, headers=self._headers
        )
        resp.raise_for_status()
        return resp.json()


def _cap(value) -> str | None:
    if not value or not isinstance(value, str):
        return None
    return value[:TEXT_CAP]


def to_record(bm: dict) -> dict:
    """Map one Karakeep bookmark JSON object to an upsert_items record."""
    content = bm.get("content") or {}
    url = content.get("url")
    title = (
        bm.get("title")
        or content.get("title")
        or url
        or _cap(content.get("text"))
        or None
    )
    if title:
        title = title[:200]
    tags = [
        t["name"] for t in bm.get("tags") or []
        if isinstance(t, dict) and t.get("name")
    ]
    url_norm = urlnorm.normalize_url(url)
    meta = {
        "type": content.get("type"),
        "tags": tags,
        "description": _cap(content.get("description")),
        "note": _cap(bm.get("note")),
        "summary": _cap(bm.get("summary")),
        "text": _cap(content.get("text")),
        "favourited": bm.get("favourited"),
        "archived": bm.get("archived"),
    }
    meta = {k: v for k, v in meta.items() if v not in (None, "", [], False)}
    return {
        "external_id": bm["id"],
        "title": title,
        "url": url,
        "url_norm": url_norm,
        "doi": urlnorm.normalize_doi(url_norm),
        "created_at": bm.get("createdAt"),
        "updated_at": bm.get("modifiedAt") or bm.get("createdAt"),
        "meta": meta,
    }


def sync(
    *,
    full: bool = False,
    api_key: str | None = None,
    client: httpx.Client | None = None,
) -> dict:
    """Pull Karakeep bookmarks into the items registry. Returns stats.

    ``api_key`` / ``client`` are injection points for tests (no Keychain, no
    network). On failure the error is recorded in sync_state.last_error and
    re-raised; the stored cursor is left unchanged.
    """
    base_url = os.environ.get("CAIRN_KARAKEEP_URL")
    if not base_url:
        raise ConnectorError("CAIRN_KARAKEEP_URL is not set")
    state = db.get_sync_state(SOURCE)
    last_created = None if full else (state or {}).get("cursor", {}).get("last_created_at")

    try:
        kc = KarakeepClient(base_url, api_key or keychain.get_secret(SERVICE), client)
        records: list[dict] = []
        newest = last_created
        cursor = None
        while True:
            page = kc.page(cursor)
            bookmarks = page.get("bookmarks") or []
            for bm in bookmarks:
                rec = to_record(bm)
                if rec["created_at"] and (newest is None or rec["created_at"] > newest):
                    newest = rec["created_at"]
                records.append(rec)
            if last_created and bookmarks and all(
                (bm.get("createdAt") or "") <= last_created for bm in bookmarks
            ):
                break  # boundary page already appended; older pages are synced
            cursor = page.get("nextCursor")
            if not cursor:
                break
        stats = db.upsert_items(SOURCE, "bookmark", records)
    except Exception as exc:
        db.set_sync_state(SOURCE, error=f"{type(exc).__name__}: {exc}")
        raise

    links = db.rebuild_item_links() if stats["changed_ids"] else None
    db.set_sync_state(SOURCE, cursor={"last_created_at": newest} if newest else {})
    return {
        "source": SOURCE,
        "fetched": len(records),
        "inserted": stats["inserted"],
        "updated": stats["updated"],
        "skipped": stats["skipped"],
        "links": links,
        "full": full,
    }
