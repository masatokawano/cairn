"""Read-only Zotero connector (M1, DESIGN.md §5.1).

Top-level bibliography items → items(kind='reference', source='zotero').
Bibliographic fields only — title, abstract, tags, creators (§4). PDF
full text and WebDAV storage are never touched (§8 non-goal 9).

Config: ``CAIRN_ZOTERO_USER_ID`` (required),
``CAIRN_ZOTERO_API_URL`` (default https://api.zotero.org).
Auth: Keychain service ``brain-sync-zotero`` (D8).

Incremental model (DESIGN.md §5.1: library version as the cursor): every
response carries ``Last-Modified-Version``; we request
``/users/{id}/items/top?since=<stored version>`` so only items changed after
the last sync come back, and store the new library version on success.
Deletions are not mirrored in M1 — a stale registry row for a deleted
Zotero item is harmless and disappears on a future full rebuild.
"""
from __future__ import annotations

import os

import httpx

from .. import db
from ..core import keychain, urlnorm
from . import ConnectorError

SERVICE = "brain-sync-zotero"
SOURCE = "zotero"
API_DEFAULT = "https://api.zotero.org"
PAGE_LIMIT = 100
TIMEOUT = 30.0

# Child/annotation types that can still surface via /items/top edge cases.
_SKIP_TYPES = {"attachment", "note", "annotation"}


class ZoteroClient:
    """Minimal read-only client. GET /users/{id}/items/top only."""

    def __init__(self, api_url: str, user_id: str, api_key: str,
                 client: httpx.Client | None = None):
        self._base = api_url.rstrip("/")
        self._user_id = user_id
        self._client = client or httpx.Client(timeout=TIMEOUT)
        self._headers = {
            "Zotero-API-Key": api_key,
            "Zotero-API-Version": "3",
            "Accept": "application/json",
        }

    def page(self, start: int, since: int | None = None) -> tuple[list[dict], int]:
        """One page of top-level items. Returns (items, library_version)."""
        params: dict = {"format": "json", "limit": PAGE_LIMIT, "start": start}
        if since is not None:
            params["since"] = since
        resp = self._client.get(
            f"{self._base}/users/{self._user_id}/items/top",
            params=params, headers=self._headers,
        )
        resp.raise_for_status()
        version = int(resp.headers.get("Last-Modified-Version", "0"))
        return resp.json(), version


def _creator_name(creator: dict) -> str:
    if creator.get("name"):
        return str(creator["name"]).strip()
    first = str(creator.get("firstName") or "").strip()
    last = str(creator.get("lastName") or "").strip()
    return " ".join(p for p in (first, last) if p)


def to_record(item: dict) -> dict:
    """Map one Zotero item JSON object to an upsert_items record."""
    data = item.get("data") or {}
    url = data.get("url") or None
    url_norm = urlnorm.normalize_url(url)
    doi = urlnorm.normalize_doi(data.get("DOI")) or urlnorm.normalize_doi(url_norm)
    creators = [n for n in (_creator_name(c) for c in data.get("creators") or []) if n]
    tags = [
        t["tag"] for t in data.get("tags") or []
        if isinstance(t, dict) and t.get("tag")
    ]
    meta = {
        "itemType": data.get("itemType"),
        "creators": creators,
        "tags": tags,
        "abstract": data.get("abstractNote") or None,
        "publication": data.get("publicationTitle") or None,
        "date": data.get("date") or None,
    }
    meta = {k: v for k, v in meta.items() if v not in (None, "", [])}
    return {
        "external_id": item["key"],
        "title": (data.get("title") or None),
        "url": url,
        "url_norm": url_norm,
        "doi": doi,
        "created_at": data.get("dateAdded"),
        "updated_at": data.get("dateModified"),
        "meta": meta,
    }


def sync(
    *,
    full: bool = False,
    api_key: str | None = None,
    client: httpx.Client | None = None,
) -> dict:
    """Pull changed Zotero bibliography into the items registry.

    ``api_key`` / ``client`` are injection points for tests. On failure the
    error lands in sync_state.last_error and the old cursor survives, so the
    next run re-requests the same ``since`` window.
    """
    user_id = os.environ.get("CAIRN_ZOTERO_USER_ID")
    if not user_id:
        raise ConnectorError("CAIRN_ZOTERO_USER_ID is not set")
    api_url = os.environ.get("CAIRN_ZOTERO_API_URL", API_DEFAULT)
    state = db.get_sync_state(SOURCE)
    since = None if full else (state or {}).get("cursor", {}).get("library_version")

    try:
        zc = ZoteroClient(api_url, user_id, api_key or keychain.get_secret(SERVICE), client)
        records: list[dict] = []
        library_version = since or 0
        start = 0
        while True:
            items, version = zc.page(start, since)
            library_version = max(library_version, version)
            for item in items:
                if (item.get("data") or {}).get("itemType") in _SKIP_TYPES:
                    continue
                records.append(to_record(item))
            if len(items) < PAGE_LIMIT:
                break
            start += PAGE_LIMIT
        stats = db.upsert_items(SOURCE, "reference", records)
    except Exception as exc:
        db.set_sync_state(SOURCE, error=f"{type(exc).__name__}: {exc}")
        raise

    links = db.rebuild_item_links() if stats["changed_ids"] else None
    db.set_sync_state(SOURCE, cursor={"library_version": library_version})
    return {
        "source": SOURCE,
        "fetched": len(records),
        "inserted": stats["inserted"],
        "updated": stats["updated"],
        "skipped": stats["skipped"],
        "links": links,
        "full": full,
    }
