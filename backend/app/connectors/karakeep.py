"""Read-only Karakeep connector (M1, DESIGN.md §5.1).

Bookmarks → items(kind='bookmark', source='karakeep'). Tags, description,
notes and (capped) full text go into meta — that is the M2 indexing text
(§4: タイトル + 説明/メモ + タグ、原本複製はしない).

Config: ``CAIRN_KARAKEEP_URL`` (base URL, e.g. https://karakeep.example.com).
Auth: Keychain service ``brain-sync-karakeep`` (D8), account = login user.

Incremental model: the v1 API has no modified-since filter, so we fetch
newest-first (createdAt desc, cursor pagination) and stop once a whole page
is strictly older than the stored ``last_created_at`` — pages still touching
the boundary timestamp keep pagination alive, so a run of same-createdAt
arrivals spanning pages is not lost, and content_hash comparison turns the
overlap into skips. Edits to bookmarks older than the cursor (tag changes
etc.) are invisible to the incremental pass, so ``sync()`` promotes itself
to a full sweep once every ``FULL_SWEEP_INTERVAL_H`` hours (cursor field
``last_full_sync_at``); ``--full`` forces one. A full sweep that fetched a
complete, non-empty listing also prunes registry rows deleted upstream —
an incremental or failed run never prunes.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import httpx

from .. import db
from ..core import keychain
from . import ConnectorError, index_changed_items

SERVICE = "brain-sync-karakeep"
SOURCE = "karakeep"
PAGE_LIMIT = 100
TIMEOUT = 30.0
# Cap on free-text fields kept in meta (excerpt, not a copy of the original).
TEXT_CAP = 10_000
# Hourly syncs auto-promote to a full sweep this often, so edits/deletions of
# old bookmarks converge within a day without a separate agent.
FULL_SWEEP_INTERVAL_H = 24


def _full_sweep_due(cursor: dict) -> bool:
    last = cursor.get("last_full_sync_at")
    if not last:
        return True
    try:
        ts = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
    except ValueError:
        return True
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - ts >= timedelta(hours=FULL_SWEEP_INTERVAL_H)


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
    # url_norm / doi are derived in db.upsert_items from the redacted url —
    # never pre-computed here (redaction choke point).
    return {
        "external_id": bm["id"],
        "title": title,
        "url": url,
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
    stored = (state or {}).get("cursor", {})
    full = full or _full_sweep_due(stored)
    last_created = None if full else stored.get("last_created_at")

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
            # Stop only when the whole page is STRICTLY older than the cursor:
            # a run of bookmarks sharing the boundary createdAt can span pages,
            # and their order within the run is unspecified, so any page still
            # touching the boundary timestamp must keep pagination alive
            # (Codex M1 review, should #2).
            if last_created and bookmarks and all(
                (bm.get("createdAt") or "") < last_created for bm in bookmarks
            ):
                break
            cursor = page.get("nextCursor")
            if not cursor:
                break
        stats = db.upsert_items(SOURCE, "bookmark", records)
        # On a full sweep the loop above only ends at nextCursor=None (the
        # early-stop needs a cursor), so `records` is the complete upstream
        # listing — bookmarks deleted in Karakeep can be pruned. An empty
        # listing is left alone (API 異常で全消ししない).
        pruned = 0
        if full and records:
            pruned = db.prune_items(
                SOURCE, keep_external_ids=[r["external_id"] for r in records]
            )
    except Exception as exc:
        db.set_sync_state(SOURCE, error=f"{type(exc).__name__}: {exc}")
        raise

    index_stats = index_changed_items(stats["changed_ids"]) if stats["changed_ids"] else None
    links = db.rebuild_item_links() if stats["changed_ids"] or pruned else None
    cursor_out = {"last_created_at": newest} if newest else {}
    if full:
        cursor_out["last_full_sync_at"] = db.utcnow_iso()
    elif stored.get("last_full_sync_at"):
        cursor_out["last_full_sync_at"] = stored["last_full_sync_at"]
    db.set_sync_state(SOURCE, cursor=cursor_out)
    return {
        "source": SOURCE,
        "fetched": len(records),
        "inserted": stats["inserted"],
        "updated": stats["updated"],
        "skipped": stats["skipped"],
        "pruned": pruned,
        "index": index_stats,
        "links": links,
        "full": full,
    }
