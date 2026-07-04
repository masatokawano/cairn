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


class ConnectorError(RuntimeError):
    """Connector-level failure (missing config, API error). Messages must
    never contain API keys."""
