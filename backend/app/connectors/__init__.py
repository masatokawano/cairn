"""Read-only clients for Karakeep, Zotero, and Obsidian.

karakeep / zotero land in M1; obsidian in M3. All connectors here MUST be
read-only against external systems (invariant 1 in AGENTS.md); Cairn never
writes back to Karakeep, Zotero, or the original conversation stores.

Shared contract (DESIGN.md §5.1): API keys come from the macOS Keychain via
core.keychain (never from config/env). Each connector's ``sync()`` reads its
cursor from ``sync_state``, fetches incrementally, upserts into ``items``
via db.upsert_items (which applies secret redaction), regenerates
``item_links`` when anything changed, and records the outcome in
``sync_state`` — last_error on failure, with the previous cursor kept so the
next run retries the same window.
"""
from __future__ import annotations

import logging

log = logging.getLogger("cairn.connectors")


class ConnectorError(RuntimeError):
    """Connector-level failure (missing config, API error). Messages must
    never contain API keys."""


def index_changed_items(changed_ids: list[int]) -> dict:
    """Chunk + (best-effort) embed the items an upsert just changed (M2).

    Chunking is mandatory — without it keyword search can't see the item.
    Embedding is best-effort: it needs the sentence-transformers runtime and
    an existing (provider, model), and a missing model must not fail an
    hourly sync (S4). Un-embedded chunks are picked up later by
    `cairn index rebuild` / `admin reindex`; until then the item is
    keyword-searchable only. Only the freshly written chunk ids are embedded
    so a sync never turns into a whole-archive backfill.
    """
    from .. import db

    chunk_stats = db.rechunk_items(changed_ids, force=True)
    embedded: int | None = None
    if chunk_stats["chunk_ids"]:
        try:
            provider = db._active_embedding_provider()
            embedded = db.embed_chunks(
                provider, chunk_ids=chunk_stats["chunk_ids"], only_missing=True,
            )["chunks"]
        except Exception as exc:
            # degraded, not failed: record why in the sync stats + log
            log.warning("embedding skipped for %d chunks: %s",
                        len(chunk_stats["chunk_ids"]), exc)
            embedded = None
    return {
        "chunks": chunk_stats["chunks"],
        "embedded": embedded,
    }
