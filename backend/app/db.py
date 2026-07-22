"""SQLite layer: schema, diff import, and search (FTS5 trigram + LIKE fallback)."""
from __future__ import annotations

import datetime
import glob
import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import threading

from . import redact
from .chunking import CURRENT_CHUNKING_VERSION, chunk_text
from .core import urlnorm
from .embedding import EmbeddingProvider
from .vector_index import NumpyIndex, SQLiteVecIndex, VectorIndex, try_load_sqlite_vec

log = logging.getLogger("cairn.db")

DB_PATH = os.environ.get(
    "CAIRN_DB", os.path.join(os.path.dirname(__file__), "..", "data", "cairn.db")
)

_local = threading.local()

# Set by connect() per thread; reflects the actual outcome of attempting to
# load the sqlite-vec extension on that connection. False means the
# NumpyIndex fallback path is in use even if sqlite-vec is installed.
def _sqlite_vec_loaded() -> bool:
    return getattr(_local, "sqlite_vec_loaded", False)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    source_id TEXT NOT NULL,
    title TEXT NOT NULL,
    created_at TEXT,
    updated_at TEXT,
    content_hash TEXT NOT NULL,
    meta TEXT NOT NULL DEFAULT '{}',
    UNIQUE(source, source_id)
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    idx INTEGER NOT NULL,
    role TEXT NOT NULL,
    text TEXT NOT NULL,
    created_at TEXT,
    source_message_id TEXT  -- stable per-message id from the source, when available
);
CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id);
CREATE INDEX IF NOT EXISTS idx_conversations_updated ON conversations(updated_at);

-- trigram: substring matching that works for Japanese; queries shorter than
-- 3 chars fall back to LIKE in search() below.
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    text,
    content='messages',
    content_rowid='id',
    tokenize='trigram'
);
CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;
CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE OF text ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, text) VALUES ('delete', old.id, old.text);
    INSERT INTO messages_fts(rowid, text) VALUES (new.id, new.text);
END;

CREATE TABLE IF NOT EXISTS ingest_files (
    path TEXT PRIMARY KEY,
    mtime REAL NOT NULL,
    size INTEGER NOT NULL
);

-- Attachment metadata (P1-H). Cairn stores metadata only; the bytes
-- themselves are not kept (avoids ballooning the DB and keeps redaction's
-- scope to text). hash = sha256 of the decoded bytes, so dedup / change
-- detection is by content. message_id is nullable to allow conversation-
-- level attachments in the future; conversation_id is required.
-- extracted_text is reserved for future OCR / PDF text extraction.
CREATE TABLE IF NOT EXISTS attachments (
    id INTEGER PRIMARY KEY,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    message_id INTEGER REFERENCES messages(id) ON DELETE CASCADE,
    source_ref TEXT,
    mime TEXT,
    size INTEGER,
    hash TEXT,
    extracted_text TEXT
);
CREATE INDEX IF NOT EXISTS idx_attachments_conv ON attachments(conversation_id);
CREATE INDEX IF NOT EXISTS idx_attachments_msg ON attachments(message_id);

-- Import history (P1-B): one row per ingest of a single input (an uploaded
-- file, or one CLI log file). Records counts, warnings, parser version, and
-- the input's content hash for auditability.
-- failed (backlog A2) = パースできなかった入力単位の数: ファイル/upload 全体の
-- 例外 → 1、複数シャード zip → 失敗シャード数。パーサ内の寛容な per-entry
-- skip は warnings のままで failed に数えない。
CREATE TABLE IF NOT EXISTS import_runs (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,          -- "upload" | "claude_cli" | "codex_cli"
    input_name TEXT,              -- uploaded filename or log path
    started_at TEXT NOT NULL,
    completed_at TEXT,
    parser_version TEXT,
    inserted INTEGER NOT NULL DEFAULT 0,
    updated INTEGER NOT NULL DEFAULT 0,
    skipped INTEGER NOT NULL DEFAULT 0,
    failed INTEGER NOT NULL DEFAULT 0,
    conversations INTEGER NOT NULL DEFAULT 0,
    warnings INTEGER NOT NULL DEFAULT 0,
    warning_summary TEXT,
    content_hash TEXT,
    status TEXT NOT NULL DEFAULT 'ok',  -- "ok" | "error"
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_import_runs_started ON import_runs(started_at);

-- Item registry (v11, DESIGN.md §4): cross-source registry over conversations,
-- bookmarks (karakeep, M1), references (zotero, M1), and notes (obsidian, M3).
-- Search / recall / linking all pivot on items. Kind-specific detail lives in
-- the per-kind tables (conversations here; future karakeep/zotero/obsidian
-- item tables under M1/M3). social_post (v13, ADR-0006): self-authored
-- X/Facebook posts, replies and comments from official export archives.
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('conversation','bookmark','reference','note','social_post')),
    source TEXT NOT NULL,
    external_id TEXT NOT NULL,
    title TEXT,
    url TEXT,
    url_norm TEXT,
    doi TEXT,
    created_at TEXT,
    updated_at TEXT,
    content_hash TEXT,
    meta TEXT,
    UNIQUE (source, external_id)
);
CREATE INDEX IF NOT EXISTS idx_items_url_norm ON items(url_norm) WHERE url_norm IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_items_doi      ON items(doi)      WHERE doi IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_items_updated  ON items(kind, updated_at);

-- Strong-match links between items (v11, DESIGN.md §5 D5): URL/DOI/GitHub
-- exact matches after normalisation. Only these three link types are stored;
-- same-source similarity is computed at query time via RRF and never persisted.
-- The a_id < b_id check keeps each undirected pair as a single row.
CREATE TABLE IF NOT EXISTS item_links (
    a_id     INTEGER NOT NULL REFERENCES items(id),
    b_id     INTEGER NOT NULL REFERENCES items(id),
    link_via TEXT NOT NULL CHECK (link_via IN ('url','doi','github')),
    PRIMARY KEY (a_id, b_id, link_via),
    CHECK (a_id < b_id)
);

-- External-source sync cursors (v11, DESIGN.md §4): replaces the legacy
-- brain-sync state.json files. Populated by connectors under M1/M3.
CREATE TABLE IF NOT EXISTS sync_state (
    source     TEXT PRIMARY KEY,
    cursor     TEXT NOT NULL,
    synced_at  TEXT NOT NULL,
    last_error TEXT
);

-- Chunks (P2-1a): derived units of message text for semantic search. A message
-- is one chunk unless it exceeds the chunking window, in which case it is split
-- (see app/chunking.py). Each row records char offsets into the original
-- message.text so the source span is recoverable. Chunks are derived data:
-- droppable and regenerable from messages via rechunk_messages(). The
-- chunking_version column lets a new algorithm's chunks coexist with the old
-- during a staged re-generation. item_id (v11) is the cross-source join key;
-- for pre-M1 chunks all items are kind='conversation' and item_id maps 1:1 to
-- conversation_id via items(source, external_id).
CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY,
    -- v12: message_id / conversation_id are NULL for external-item chunks
    -- (kind='item_text'); the CHECK keeps every chunk anchored to either a
    -- message (conversation path) or an item (external path).
    message_id INTEGER REFERENCES messages(id) ON DELETE CASCADE,
    conversation_id INTEGER REFERENCES conversations(id) ON DELETE CASCADE,
    idx INTEGER NOT NULL,
    start_offset INTEGER NOT NULL,
    end_offset INTEGER NOT NULL,
    text TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'message_text',  -- "message_text" | "attachment_text" | "item_text"
    chunking_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    item_id INTEGER REFERENCES items(id),  -- v11; cross-source join key
    CHECK ((message_id IS NOT NULL AND conversation_id IS NOT NULL) OR item_id IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS idx_chunks_msg ON chunks(message_id);
CREATE INDEX IF NOT EXISTS idx_chunks_conv ON chunks(conversation_id);

-- FTS over external-item chunks only (v12, M2). Standalone (not external
-- content) on purpose: the index is PARTIAL (kind='item_text' rows only), and
-- an external-content 'rebuild' command would re-index the whole chunks table
-- including message chunks, duplicating messages_fts hits. Standalone FTS
-- supports plain DELETE, so partial rebuilds stay correct; the duplicated
-- text is small (bookmark/reference excerpts, not conversation bodies).
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    text,
    tokenize='trigram'
);
CREATE TRIGGER IF NOT EXISTS chunks_fts_ai AFTER INSERT ON chunks
WHEN new.kind = 'item_text' BEGIN
    INSERT INTO chunks_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS chunks_fts_ad AFTER DELETE ON chunks
WHEN old.kind = 'item_text' BEGIN
    DELETE FROM chunks_fts WHERE rowid = old.id;
END;
CREATE TRIGGER IF NOT EXISTS chunks_fts_au AFTER UPDATE OF text ON chunks
WHEN old.kind = 'item_text' BEGIN
    DELETE FROM chunks_fts WHERE rowid = old.id;
    INSERT INTO chunks_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE INDEX IF NOT EXISTS idx_chunks_version ON chunks(chunking_version);
CREATE INDEX IF NOT EXISTS idx_chunks_item ON chunks(item_id);

-- Embeddings (P2-1b): one vector per (chunk, provider, model). Vectors live
-- as f32 little-endian BLOBs; dimension is stored per row so a query knows
-- the width without consulting the provider. Multiple provider/model rows
-- per chunk are intentional (A/B and migration). Embeddings are derived data:
-- droppable and regenerable from chunks via embed_chunks().
CREATE TABLE IF NOT EXISTS embeddings (
    id INTEGER PRIMARY KEY,
    chunk_id INTEGER NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    dimension INTEGER NOT NULL,
    vector BLOB NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(chunk_id, provider, model)
);
CREATE INDEX IF NOT EXISTS idx_embeddings_chunk ON embeddings(chunk_id);
CREATE INDEX IF NOT EXISTS idx_embeddings_provider_model ON embeddings(provider, model);

-- Extraction runs (P3-A): one row per LLM extraction batch, analogous to
-- import_runs. Records provider, model, prompt version, token counts, retries,
-- and outcome so cost and quality can be audited over time.
CREATE TABLE IF NOT EXISTS extraction_runs (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,              -- "rules-entity"|"segment"|"assertion"|"artifact"
    scope TEXT NOT NULL,             -- "conversation:{id}"|"segment:{id}"|"all"
    provider TEXT NOT NULL,          -- "ollama"|"anthropic"|"rules"|"fixture"
    model TEXT,                      -- model id, NULL for rules-based
    prompt_version TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    input_token_count INTEGER,
    output_token_count INTEGER,
    retries INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'running',  -- running|ok|partial|failed
    error TEXT,
    warnings INTEGER NOT NULL DEFAULT 0,
    warning_summary TEXT
);
CREATE INDEX IF NOT EXISTS idx_extraction_runs_kind ON extraction_runs(kind, started_at);
CREATE INDEX IF NOT EXISTS idx_extraction_runs_status ON extraction_runs(status);

-- Entities (P3-B): deduplicated canonical entities discovered across all
-- conversations. Each unique (kind, canonical_name) pair is one row.
CREATE TABLE IF NOT EXISTS entities (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,               -- url|repo|doi|arxiv|person|org|project|product|place
    canonical_name TEXT NOT NULL,     -- normalised form (lowercase domain, stripped path, etc.)
    external_id TEXT,                 -- owner/repo for repos, domain for URLs, DOI etc.
    meta TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE(kind, canonical_name)
);
CREATE INDEX IF NOT EXISTS idx_entities_kind ON entities(kind);
CREATE INDEX IF NOT EXISTS idx_entities_external_id ON entities(external_id);

-- Entity mentions (P3-B): per-message occurrences of entities with char offsets.
CREATE TABLE IF NOT EXISTS entity_mentions (
    id INTEGER PRIMARY KEY,
    entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    start_offset INTEGER NOT NULL,
    end_offset INTEGER NOT NULL,
    surface TEXT NOT NULL,            -- raw text as it appears in the message
    detector TEXT NOT NULL,           -- "rules-url-v1" | "rules-repo-v1"
    created_at TEXT NOT NULL,
    UNIQUE(entity_id, message_id, start_offset)
);
CREATE INDEX IF NOT EXISTS idx_entity_mentions_msg ON entity_mentions(message_id);
CREATE INDEX IF NOT EXISTS idx_entity_mentions_conv ON entity_mentions(conversation_id);
CREATE INDEX IF NOT EXISTS idx_entity_mentions_entity ON entity_mentions(entity_id);

-- Segments (P3-C): one row per conversation segment (topic-coherent block of
-- messages). Generated by LLM; locked_by_user protects manual edits from
-- batch regeneration.
CREATE TABLE IF NOT EXISTS segments (
    id INTEGER PRIMARY KEY,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    idx INTEGER NOT NULL,
    start_message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    end_message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    summary TEXT NOT NULL,
    topics TEXT NOT NULL DEFAULT '[]',
    generated_by TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    extraction_run_id INTEGER REFERENCES extraction_runs(id) ON DELETE SET NULL,
    locked_by_user INTEGER NOT NULL DEFAULT 0,
    user_edited_at TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(conversation_id, idx)
);
CREATE INDEX IF NOT EXISTS idx_segments_conv ON segments(conversation_id);
CREATE INDEX IF NOT EXISTS idx_segments_lock ON segments(locked_by_user);

-- Assertions (P3-D): factual claims, decisions, questions etc. extracted from
-- segments by LLM. actor/kind/status are app-layer validated (no CHECK constraint).
CREATE TABLE IF NOT EXISTS assertions (
    id INTEGER PRIMARY KEY,
    segment_id INTEGER NOT NULL REFERENCES segments(id) ON DELETE CASCADE,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    text TEXT NOT NULL,
    actor TEXT NOT NULL,              -- user|assistant|shared
    kind TEXT NOT NULL,               -- claim|hypothesis|conclusion|decision|rejected_idea|question|todo
    status TEXT NOT NULL DEFAULT 'tentative',  -- tentative|accepted|rejected|superseded|unresolved|completed
    confidence REAL,
    supporting_message_ids TEXT NOT NULL DEFAULT '[]',
    superseded_by_assertion_id INTEGER REFERENCES assertions(id) ON DELETE SET NULL,
    generated_by TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    extraction_run_id INTEGER REFERENCES extraction_runs(id) ON DELETE SET NULL,
    locked_by_user INTEGER NOT NULL DEFAULT 0,
    user_edited_at TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_assertions_seg ON assertions(segment_id);
CREATE INDEX IF NOT EXISTS idx_assertions_conv ON assertions(conversation_id);
CREATE INDEX IF NOT EXISTS idx_assertions_actor_kind ON assertions(actor, kind);
CREATE INDEX IF NOT EXISTS idx_assertions_status ON assertions(status);
CREATE INDEX IF NOT EXISTS idx_assertions_lock ON assertions(locked_by_user);
"""

_MIGRATION_2_IMPORT_RUNS = """
CREATE TABLE IF NOT EXISTS import_runs (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    input_name TEXT,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    parser_version TEXT,
    inserted INTEGER NOT NULL DEFAULT 0,
    updated INTEGER NOT NULL DEFAULT 0,
    skipped INTEGER NOT NULL DEFAULT 0,
    failed INTEGER NOT NULL DEFAULT 0,
    conversations INTEGER NOT NULL DEFAULT 0,
    warnings INTEGER NOT NULL DEFAULT 0,
    warning_summary TEXT,
    content_hash TEXT,
    status TEXT NOT NULL DEFAULT 'ok',
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_import_runs_started ON import_runs(started_at);
"""


# Schema versioning via PRAGMA user_version.
#
# Two complementary mechanisms:
#   _SCHEMA       — always the LATEST shape, idempotent (IF NOT EXISTS). A
#                   *fresh* DB is built from this and stamped to the current
#                   version directly; it never runs migrations.
#   _MIGRATIONS   — ordered (version, sql) steps that transform an *existing*
#                   DB whose data IF NOT EXISTS cannot fix (e.g. ALTER TABLE
#                   ADD COLUMN, backfills). Append a step and bump
#                   _SCHEMA_VERSION together. When a migration runs, a backup
#                   of the DB is taken first.
#
# Order in connect(): existing DBs run _MIGRATIONS *before* _SCHEMA (see
# connect() below). _SCHEMA-after-migrations means the latest shape may
# reference tables/columns that only exist after migrations complete
# (e.g. v11's idx_chunks_item ON chunks.item_id). Reversing this order —
# _SCHEMA first — would fail on any pre-latest DB the moment a new index in
# _SCHEMA points at a not-yet-migrated column.
_MIGRATION_3_MSG_SOURCE_ID = (
    "ALTER TABLE messages ADD COLUMN source_message_id TEXT;"
)

_MIGRATION_4_ATTACHMENTS = """
CREATE TABLE IF NOT EXISTS attachments (
    id INTEGER PRIMARY KEY,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    message_id INTEGER REFERENCES messages(id) ON DELETE CASCADE,
    source_ref TEXT,
    mime TEXT,
    size INTEGER,
    hash TEXT,
    extracted_text TEXT
);
CREATE INDEX IF NOT EXISTS idx_attachments_conv ON attachments(conversation_id);
CREATE INDEX IF NOT EXISTS idx_attachments_msg ON attachments(message_id);
"""

_MIGRATION_5_CHUNKS = """
CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY,
    message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    idx INTEGER NOT NULL,
    start_offset INTEGER NOT NULL,
    end_offset INTEGER NOT NULL,
    text TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'message_text',
    chunking_version TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunks_msg ON chunks(message_id);
CREATE INDEX IF NOT EXISTS idx_chunks_conv ON chunks(conversation_id);
CREATE INDEX IF NOT EXISTS idx_chunks_version ON chunks(chunking_version);
"""

_MIGRATION_6_EMBEDDINGS = """
CREATE TABLE IF NOT EXISTS embeddings (
    id INTEGER PRIMARY KEY,
    chunk_id INTEGER NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    dimension INTEGER NOT NULL,
    vector BLOB NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(chunk_id, provider, model)
);
CREATE INDEX IF NOT EXISTS idx_embeddings_chunk ON embeddings(chunk_id);
CREATE INDEX IF NOT EXISTS idx_embeddings_provider_model ON embeddings(provider, model);
"""

_MIGRATION_7_EXTRACTION_RUNS = """
CREATE TABLE IF NOT EXISTS extraction_runs (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    scope TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT,
    prompt_version TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    input_token_count INTEGER,
    output_token_count INTEGER,
    retries INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'running',
    error TEXT,
    warnings INTEGER NOT NULL DEFAULT 0,
    warning_summary TEXT
);
CREATE INDEX IF NOT EXISTS idx_extraction_runs_kind ON extraction_runs(kind, started_at);
CREATE INDEX IF NOT EXISTS idx_extraction_runs_status ON extraction_runs(status);
"""

_MIGRATION_10_ASSERTIONS = """
CREATE TABLE IF NOT EXISTS assertions (
    id INTEGER PRIMARY KEY,
    segment_id INTEGER NOT NULL REFERENCES segments(id) ON DELETE CASCADE,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    text TEXT NOT NULL,
    actor TEXT NOT NULL,
    kind TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'tentative',
    confidence REAL,
    supporting_message_ids TEXT NOT NULL DEFAULT '[]',
    superseded_by_assertion_id INTEGER REFERENCES assertions(id) ON DELETE SET NULL,
    generated_by TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    extraction_run_id INTEGER REFERENCES extraction_runs(id) ON DELETE SET NULL,
    locked_by_user INTEGER NOT NULL DEFAULT 0,
    user_edited_at TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_assertions_seg ON assertions(segment_id);
CREATE INDEX IF NOT EXISTS idx_assertions_conv ON assertions(conversation_id);
CREATE INDEX IF NOT EXISTS idx_assertions_actor_kind ON assertions(actor, kind);
CREATE INDEX IF NOT EXISTS idx_assertions_status ON assertions(status);
CREATE INDEX IF NOT EXISTS idx_assertions_lock ON assertions(locked_by_user);
"""

_MIGRATION_9_SEGMENTS = """
CREATE TABLE IF NOT EXISTS segments (
    id INTEGER PRIMARY KEY,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    idx INTEGER NOT NULL,
    start_message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    end_message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    summary TEXT NOT NULL,
    topics TEXT NOT NULL DEFAULT '[]',
    generated_by TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    extraction_run_id INTEGER REFERENCES extraction_runs(id) ON DELETE SET NULL,
    locked_by_user INTEGER NOT NULL DEFAULT 0,
    user_edited_at TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(conversation_id, idx)
);
CREATE INDEX IF NOT EXISTS idx_segments_conv ON segments(conversation_id);
CREATE INDEX IF NOT EXISTS idx_segments_lock ON segments(locked_by_user);
"""

_MIGRATION_8_ENTITIES = """
CREATE TABLE IF NOT EXISTS entities (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    canonical_name TEXT NOT NULL,
    external_id TEXT,
    meta TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE(kind, canonical_name)
);
CREATE INDEX IF NOT EXISTS idx_entities_kind ON entities(kind);
CREATE INDEX IF NOT EXISTS idx_entities_external_id ON entities(external_id);
CREATE TABLE IF NOT EXISTS entity_mentions (
    id INTEGER PRIMARY KEY,
    entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    start_offset INTEGER NOT NULL,
    end_offset INTEGER NOT NULL,
    surface TEXT NOT NULL,
    detector TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(entity_id, message_id, start_offset)
);
CREATE INDEX IF NOT EXISTS idx_entity_mentions_msg ON entity_mentions(message_id);
CREATE INDEX IF NOT EXISTS idx_entity_mentions_conv ON entity_mentions(conversation_id);
CREATE INDEX IF NOT EXISTS idx_entity_mentions_entity ON entity_mentions(entity_id);
"""

# Migration 11 (DESIGN.md §4, M0): add items registry, item_links, sync_state,
# and chunks.item_id. Backfill items rows for every existing conversation and
# link each existing chunk to its item. All statements are wrapped in a single
# BEGIN/COMMIT so a partial failure rolls back cleanly — this is the first
# migration containing a non-idempotent ALTER (duplicate-column would otherwise
# permanently jam re-runs). The IF NOT EXISTS / WHERE NOT EXISTS / IS NULL
# guards are additional safety nets; correctness relies on the transaction.
_MIGRATION_11_ITEMS = """
BEGIN;
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('conversation','bookmark','reference','note')),
    source TEXT NOT NULL,
    external_id TEXT NOT NULL,
    title TEXT,
    url TEXT,
    url_norm TEXT,
    doi TEXT,
    created_at TEXT,
    updated_at TEXT,
    content_hash TEXT,
    meta TEXT,
    UNIQUE (source, external_id)
);
CREATE INDEX IF NOT EXISTS idx_items_url_norm ON items(url_norm) WHERE url_norm IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_items_doi      ON items(doi)      WHERE doi IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_items_updated  ON items(kind, updated_at);
CREATE TABLE IF NOT EXISTS item_links (
    a_id     INTEGER NOT NULL REFERENCES items(id),
    b_id     INTEGER NOT NULL REFERENCES items(id),
    link_via TEXT NOT NULL CHECK (link_via IN ('url','doi','github')),
    PRIMARY KEY (a_id, b_id, link_via),
    CHECK (a_id < b_id)
);
CREATE TABLE IF NOT EXISTS sync_state (
    source     TEXT PRIMARY KEY,
    cursor     TEXT NOT NULL,
    synced_at  TEXT NOT NULL,
    last_error TEXT
);
ALTER TABLE chunks ADD COLUMN item_id INTEGER REFERENCES items(id);
CREATE INDEX IF NOT EXISTS idx_chunks_item ON chunks(item_id);
INSERT INTO items (kind, source, external_id, title, created_at, updated_at, content_hash, meta)
SELECT 'conversation', c.source, c.source_id, c.title, c.created_at, c.updated_at, c.content_hash, c.meta
FROM conversations c
WHERE NOT EXISTS (
    SELECT 1 FROM items i WHERE i.source = c.source AND i.external_id = c.source_id
);
UPDATE chunks
SET item_id = (
    SELECT i.id FROM items i
    JOIN conversations c ON c.id = chunks.conversation_id
    WHERE i.source = c.source AND i.external_id = c.source_id
)
WHERE item_id IS NULL;
COMMIT;
"""

# Migration 12 (DESIGN.md §7 M2): rebuild chunks so external items can be
# chunked — message_id / conversation_id become nullable with a CHECK anchor
# (message or item), and chunks_fts (partial FTS over kind='item_text') is
# added. chunks is DERIVED data (rebuildable via rechunk), so a table rebuild
# does not touch invariant 3's originals; ids are copied verbatim so
# embeddings.chunk_id and the vec0 mirror stay valid. foreign_keys must be OFF
# around the DROP/RENAME: embeddings has ON DELETE CASCADE on chunk_id, and
# dropping the old table with FK enforcement on would cascade-delete every
# embedding. executescript commits any open transaction first, so the PRAGMA
# lands outside BEGIN as required.
_MIGRATION_12_ITEM_CHUNKS = """
PRAGMA foreign_keys=OFF;
BEGIN;
CREATE TABLE chunks_v12 (
    id INTEGER PRIMARY KEY,
    message_id INTEGER REFERENCES messages(id) ON DELETE CASCADE,
    conversation_id INTEGER REFERENCES conversations(id) ON DELETE CASCADE,
    idx INTEGER NOT NULL,
    start_offset INTEGER NOT NULL,
    end_offset INTEGER NOT NULL,
    text TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'message_text',
    chunking_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    item_id INTEGER REFERENCES items(id),
    CHECK ((message_id IS NOT NULL AND conversation_id IS NOT NULL) OR item_id IS NOT NULL)
);
INSERT INTO chunks_v12
    SELECT id, message_id, conversation_id, idx, start_offset, end_offset,
           text, kind, chunking_version, created_at, item_id
    FROM chunks;
DROP TABLE chunks;
ALTER TABLE chunks_v12 RENAME TO chunks;
CREATE INDEX IF NOT EXISTS idx_chunks_msg ON chunks(message_id);
CREATE INDEX IF NOT EXISTS idx_chunks_conv ON chunks(conversation_id);
CREATE INDEX IF NOT EXISTS idx_chunks_version ON chunks(chunking_version);
CREATE INDEX IF NOT EXISTS idx_chunks_item ON chunks(item_id);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    text,
    tokenize='trigram'
);
CREATE TRIGGER IF NOT EXISTS chunks_fts_ai AFTER INSERT ON chunks
WHEN new.kind = 'item_text' BEGIN
    INSERT INTO chunks_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS chunks_fts_ad AFTER DELETE ON chunks
WHEN old.kind = 'item_text' BEGIN
    DELETE FROM chunks_fts WHERE rowid = old.id;
END;
CREATE TRIGGER IF NOT EXISTS chunks_fts_au AFTER UPDATE OF text ON chunks
WHEN old.kind = 'item_text' BEGIN
    DELETE FROM chunks_fts WHERE rowid = old.id;
    INSERT INTO chunks_fts(rowid, text) VALUES (new.id, new.text);
END;
DELETE FROM chunks_fts;
INSERT INTO chunks_fts(rowid, text)
    SELECT id, text FROM chunks WHERE kind = 'item_text';
COMMIT;
PRAGMA foreign_keys=ON;
"""
# ^ the DELETE makes the backfill re-runnable: if user_version is rolled back
# to 11 on an already-v12 DB that has item_text chunks, a plain INSERT would
# collide on rowid (Codex M2 review, should #2). Everything runs in one
# transaction, so a crashed first run leaves no partial state either.

# Migration 13 (ADR-0006): widen items.kind CHECK to admit 'social_post'
# (self-authored X/Facebook content from official export archives). SQLite
# cannot ALTER a CHECK constraint, so this is a table rebuild — the same
# proven pattern as migration 12's chunks rebuild. items is DERIVED data
# (invariant 3: rebuildable from originals + external sources), a premigrate
# backup is taken automatically, and ids are copied verbatim so item_links
# (a_id/b_id) and chunks.item_id stay valid. foreign_keys must be OFF around
# DROP/RENAME for the same reason as v12: item_links/chunks reference items
# by name and must not cascade or block during the swap.
_MIGRATION_13_SOCIAL_KIND = """
PRAGMA foreign_keys=OFF;
BEGIN;
CREATE TABLE items_v13 (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('conversation','bookmark','reference','note','social_post')),
    source TEXT NOT NULL,
    external_id TEXT NOT NULL,
    title TEXT,
    url TEXT,
    url_norm TEXT,
    doi TEXT,
    created_at TEXT,
    updated_at TEXT,
    content_hash TEXT,
    meta TEXT,
    UNIQUE (source, external_id)
);
INSERT INTO items_v13
    SELECT id, kind, source, external_id, title, url, url_norm, doi,
           created_at, updated_at, content_hash, meta
    FROM items;
DROP TABLE items;
ALTER TABLE items_v13 RENAME TO items;
CREATE INDEX IF NOT EXISTS idx_items_url_norm ON items(url_norm) WHERE url_norm IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_items_doi      ON items(doi)      WHERE doi IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_items_updated  ON items(kind, updated_at);
COMMIT;
PRAGMA foreign_keys=ON;
"""

_SCHEMA_VERSION = 13
_MIGRATIONS: list[tuple[int, str]] = [
    (2, _MIGRATION_2_IMPORT_RUNS),       # add import_runs to pre-v2 DBs
    (3, _MIGRATION_3_MSG_SOURCE_ID),     # add messages.source_message_id to pre-v3 DBs
    (4, _MIGRATION_4_ATTACHMENTS),       # add attachments table to pre-v4 DBs
    (5, _MIGRATION_5_CHUNKS),            # add chunks table to pre-v5 DBs (P2-1a)
    (6, _MIGRATION_6_EMBEDDINGS),        # add embeddings table to pre-v6 DBs (P2-1b)
    (7, _MIGRATION_7_EXTRACTION_RUNS),   # add extraction_runs table to pre-v7 DBs (P3-A)
    (8, _MIGRATION_8_ENTITIES),          # add entities + entity_mentions to pre-v8 DBs (P3-B)
    (9, _MIGRATION_9_SEGMENTS),          # add segments table to pre-v9 DBs (P3-C)
    (10, _MIGRATION_10_ASSERTIONS),      # add assertions table to pre-v10 DBs (P3-D)
    (11, _MIGRATION_11_ITEMS),           # add items/item_links/sync_state + chunks.item_id (M0, DESIGN.md §4)
    (12, _MIGRATION_12_ITEM_CHUNKS),     # rebuild chunks for external items + chunks_fts (M2, DESIGN.md §7)
    (13, _MIGRATION_13_SOCIAL_KIND),     # widen items.kind CHECK for social_post (ADR-0006)
]


def _backup_before_migration(
    conn: sqlite3.Connection, db_path: str, from_version: int, to_version: int
) -> str:
    """Copy the DB before applying migrations so an unwanted or failed
    migration can be rolled back. Checkpoint first so the copied main file is
    self-contained (no pending WAL)."""
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.Error:
        pass  # checkpoint is best-effort; copy proceeds regardless
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = f"{db_path}.premigrate-v{from_version}-to-v{to_version}-{stamp}"
    shutil.copy2(db_path, backup_path)
    try:
        os.chmod(backup_path, 0o600)  # backup holds plaintext conversation data
    except OSError:
        pass
    log.warning(
        "schema migration v%d→v%d: backup created at %s "
        "(contains plaintext; delete once the migration is confirmed)",
        from_version, to_version, backup_path,
    )
    return backup_path


def _apply_migrations(conn: sqlite3.Connection, db_path: str) -> None:
    """Bring an existing DB up to _SCHEMA_VERSION by running pending migrations.
    Called only for non-fresh DBs (see connect())."""
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    pending = [(t, sql) for t, sql in _MIGRATIONS if version < t]
    if pending:
        _backup_before_migration(conn, db_path, version, pending[-1][0])
    for target, sql in pending:
        with conn:
            conn.executescript(sql)
            conn.execute(f"PRAGMA user_version = {target}")
        version = target
    if version < _SCHEMA_VERSION:
        conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")


def _restrict_permissions(db_path: str) -> None:
    """chmod 0600 on the DB and sidecars. WAL/SHM inherit the DB file's mode
    when SQLite creates them, so fixing the main file covers future ones."""
    for suffix in ("", "-wal", "-shm"):
        path = db_path + suffix
        try:
            if os.path.exists(path):
                os.chmod(path, 0o600)
        except OSError:
            pass  # best-effort (e.g. foreign-owned file); not fatal


def connect() -> sqlite3.Connection:
    """One connection per thread (uvicorn may run handlers on a threadpool)."""
    conn = getattr(_local, "conn", None)
    if conn is None:
        db_path = os.path.abspath(DB_PATH)
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        # Overwrite deleted content with zeros so secrets don't linger in
        # free pages / WAL after deletes and updates.
        conn.execute("PRAGMA secure_delete = ON")
        # Try to load the sqlite-vec extension early; remember the outcome
        # per-thread so vector_index() can route to NumpyIndex transparently
        # when the extension isn't available. Override with CAIRN_VECTOR_INDEX=numpy.
        if os.environ.get("CAIRN_VECTOR_INDEX") == "numpy":
            _local.sqlite_vec_loaded = False
        else:
            _local.sqlite_vec_loaded = try_load_sqlite_vec(conn)
        # A fresh DB (no tables yet) is built from the latest _SCHEMA and
        # stamped directly — it must NOT run migrations meant for older shapes.
        # An existing DB (tables present, possibly pre-versioning at v0) is
        # migrated up to _SCHEMA_VERSION *before* _SCHEMA runs (see the note
        # above _MIGRATIONS): _SCHEMA may assume columns/tables that only
        # exist once pending migrations have applied.
        is_fresh = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='conversations'"
        ).fetchone()[0] == 0
        if is_fresh:
            conn.executescript(_SCHEMA)
            conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
        else:
            _apply_migrations(conn, db_path)
            conn.executescript(_SCHEMA)
        _restrict_permissions(db_path)
        _local.conn = conn
    return conn


def _capture_derived_data(conn: sqlite3.Connection, conv_id: int) -> dict:
    """Snapshot segments + assertions (with message-index offsets) before messages are deleted.

    Segments reference message IDs which change on every update. We capture the
    start/end position as a stable message idx so _restore_derived_data() can
    re-link them after new messages are inserted.
    """
    old_id_to_idx = {r[0]: r[1] for r in
                     conn.execute("SELECT id, idx FROM messages WHERE conversation_id=?",
                                  (conv_id,)).fetchall()}
    segs = conn.execute(
        """SELECT s.*, sm.idx AS start_idx, em.idx AS end_idx
           FROM segments s
           JOIN messages sm ON sm.id = s.start_message_id
           JOIN messages em ON em.id = s.end_message_id
           WHERE s.conversation_id = ?""",
        (conv_id,),
    ).fetchall()
    result: dict = {"segments": [], "old_id_to_idx": old_id_to_idx}
    for seg in segs:
        seg_dict = dict(seg)
        seg_dict["_assertions"] = [
            dict(a) for a in
            conn.execute("SELECT * FROM assertions WHERE segment_id=?", (seg["id"],)).fetchall()
        ]
        result["segments"].append(seg_dict)
    return result


def _restore_derived_data(
    conn: sqlite3.Connection, conv_id: int, captured: dict, new_msg_ids: list[int]
) -> None:
    """Re-insert segments and assertions after messages have been re-inserted.

    Segments whose start/end idx falls beyond the end of the new message list are
    silently dropped — the conversation shrank and those segments are stale.
    """
    idx_to_new_id = {i: mid for i, mid in enumerate(new_msg_ids)}
    old_id_to_idx = captured["old_id_to_idx"]
    for seg in captured["segments"]:
        start_idx, end_idx = seg["start_idx"], seg["end_idx"]
        if start_idx not in idx_to_new_id or end_idx not in idx_to_new_id:
            continue
        new_seg_id = conn.execute(
            """INSERT INTO segments
               (conversation_id, idx, start_message_id, end_message_id, title, summary, topics,
                generated_by, prompt_version, extraction_run_id, locked_by_user, user_edited_at, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (conv_id, seg["idx"], idx_to_new_id[start_idx], idx_to_new_id[end_idx],
             seg["title"], seg["summary"], seg["topics"], seg["generated_by"],
             seg["prompt_version"], seg["extraction_run_id"], seg["locked_by_user"],
             seg["user_edited_at"], seg["created_at"]),
        ).lastrowid
        for a in seg["_assertions"]:
            old_supp = json.loads(a.get("supporting_message_ids") or "[]")
            new_supp = [
                idx_to_new_id[old_id_to_idx[oid]]
                for oid in old_supp
                if oid in old_id_to_idx and old_id_to_idx[oid] in idx_to_new_id
            ]
            conn.execute(
                """INSERT INTO assertions
                   (segment_id, conversation_id, text, actor, kind, status, confidence,
                    supporting_message_ids, generated_by, prompt_version, extraction_run_id,
                    locked_by_user, user_edited_at, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (new_seg_id, conv_id, a["text"], a["actor"], a["kind"], a["status"],
                 a["confidence"], json.dumps(new_supp), a["generated_by"], a["prompt_version"],
                 a["extraction_run_id"], a["locked_by_user"], a["user_edited_at"], a["created_at"]),
            )


def _ensure_item_for_conversation(conn: sqlite3.Connection, conv_id: int) -> int:
    """Upsert an items(kind='conversation') row mirroring the conversations row
    and return items.id.

    Called from the insert/update paths of upsert_conversations() and from
    rechunk_messages() (which self-heals if a legacy DB is missing the items
    row). The skip path — content_hash unchanged — deliberately does NOT call
    this: the migration or a prior upsert already populated the row and
    re-writing the same values on every no-op sync would be pure churn.

    admin.redact-apply mutates conversations.{title,content_hash} directly and
    bypasses this helper; integrity_check surfaces the resulting drift as an
    info-only count (admin.py is frozen through M5, DESIGN.md §5.7).
    """
    row = conn.execute(
        "SELECT source, source_id, title, created_at, updated_at, content_hash, meta"
        " FROM conversations WHERE id = ?",
        (conv_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"conversation id {conv_id} not found")
    cur = conn.execute(
        """INSERT INTO items
             (kind, source, external_id, title, created_at, updated_at, content_hash, meta)
           VALUES ('conversation', ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(source, external_id) DO UPDATE SET
             title        = excluded.title,
             created_at   = excluded.created_at,
             updated_at   = excluded.updated_at,
             content_hash = excluded.content_hash,
             meta         = excluded.meta
           RETURNING id""",
        (row["source"], row["source_id"], row["title"], row["created_at"],
         row["updated_at"], row["content_hash"], row["meta"]),
    )
    return cur.fetchone()[0]


def upsert_conversations(parsed_list) -> dict:
    """Diff import: insert new, replace changed, skip unchanged conversations.

    Secret redaction happens HERE — the single choke point for every ingest
    path (file upload and CLI sync) — and BEFORE content_hash so the stored
    hash matches the stored (redacted) text and re-syncs stay stable.
    """
    conn = connect()
    stats = {"inserted": 0, "updated": 0, "skipped": 0}
    with conn:
        for pc in parsed_list:
            for m in pc.messages:
                m.text = redact.redact(m.text)
            pc.title = redact.redact_title(pc.title)
            new_hash = pc.content_hash()
            row = conn.execute(
                "SELECT id, content_hash FROM conversations WHERE source=? AND source_id=?",
                (pc.source, pc.source_id),
            ).fetchone()
            if row and row["content_hash"] == new_hash:
                stats["skipped"] += 1
                continue
            captured = None
            if row:
                # Snapshot derived extraction data before messages are wiped so we
                # can re-link them to the fresh message IDs below.
                captured = _capture_derived_data(conn, row["id"])
                conn.execute("DELETE FROM messages WHERE conversation_id=?", (row["id"],))
                conn.execute(
                    """UPDATE conversations
                       SET title=?, created_at=?, updated_at=?, content_hash=?, meta=?
                       WHERE id=?""",
                    (pc.title, pc.created_at, pc.updated_at, new_hash,
                     json.dumps(pc.meta, ensure_ascii=False), row["id"]),
                )
                conv_id = row["id"]
                stats["updated"] += 1
            else:
                cur = conn.execute(
                    """INSERT INTO conversations
                       (source, source_id, title, created_at, updated_at, content_hash, meta)
                       VALUES (?,?,?,?,?,?,?)""",
                    (pc.source, pc.source_id, pc.title, pc.created_at, pc.updated_at,
                     new_hash, json.dumps(pc.meta, ensure_ascii=False)),
                )
                conv_id = cur.lastrowid
                stats["inserted"] += 1
            # Mirror the conversation into items (M0). Every insert/update path
            # touches it; the skip path above deliberately does not (see helper
            # docstring). chunks.item_id is populated from this value below.
            item_id = _ensure_item_for_conversation(conn, conv_id)
            # messages are re-inserted on every update; attachments hang off
            # message_id via FK CASCADE, so the previous attachments were
            # already wiped when the messages were deleted above.
            msg_ids: list[int] = []
            for i, m in enumerate(pc.messages):
                cur = conn.execute(
                    "INSERT INTO messages (conversation_id, idx, role, text, created_at, source_message_id)"
                    " VALUES (?,?,?,?,?,?)",
                    (conv_id, i, m.role, m.text, m.created_at, m.source_message_id),
                )
                msg_ids.append(cur.lastrowid)
                # Chunks are derived from the (already redacted) message text.
                # Messages were freshly inserted (new ids), so no prior chunks
                # exist for them — any old ones were CASCADE-deleted with the
                # messages above. Generate at the current version.
                _store_chunks(conn, cur.lastrowid, conv_id, m.text, CURRENT_CHUNKING_VERSION, item_id)
            # Re-link segments and assertions to new message IDs (update only).
            if captured is not None:
                _restore_derived_data(conn, conv_id, captured, msg_ids)
            attachment_rows = [
                (conv_id, msg_ids[i], a.source_ref, a.mime, a.size, a.hash, a.extracted_text)
                for i, m in enumerate(pc.messages)
                for a in m.attachments
            ]
            if attachment_rows:
                conn.executemany(
                    "INSERT INTO attachments"
                    " (conversation_id, message_id, source_ref, mime, size, hash, extracted_text)"
                    " VALUES (?,?,?,?,?,?,?)",
                    attachment_rows,
                )
            # Blob store side (P1-J): persist the bytes for any attachment
            # whose parser captured them. Lazy-imported to side-step the
            # db ↔ attachments module cycle. The hash returned matches what
            # the parser already computed (assertion below is defensive —
            # a mismatch means the parser hashed the wrong bytes).
            for m in pc.messages:
                for a in m.attachments:
                    if a.data is None:
                        continue
                    from . import attachments as _store
                    stored_hash = _store.store(a.data)
                    if a.hash is not None and stored_hash != a.hash:
                        log.warning(
                            "attachment hash mismatch: parser=%s store=%s — keeping parser hash",
                            a.hash, stored_hash,
                        )
    return stats


def utcnow_iso() -> str:
    """Timestamp helper for import_runs (ISO8601, UTC)."""
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


# --- External items (M1, DESIGN.md §5.1) -------------------------------------

def get_sync_state(source: str) -> dict | None:
    """Return the sync cursor row for a connector source, cursor JSON-decoded."""
    row = connect().execute(
        "SELECT source, cursor, synced_at, last_error FROM sync_state WHERE source = ?",
        (source,),
    ).fetchone()
    if row is None:
        return None
    return {
        "source": row["source"],
        "cursor": json.loads(row["cursor"]),
        "synced_at": row["synced_at"],
        "last_error": row["last_error"],
    }


def set_sync_state(source: str, cursor: dict | None = None, error: str | None = None) -> None:
    """Record a sync outcome. Success: pass the new cursor (clears last_error).
    Failure: pass error only — the stored cursor is kept unchanged so the next
    run retries the same window (DESIGN.md §5.1: 既存データは壊さない)."""
    conn = connect()
    with conn:
        if error is None:
            conn.execute(
                """INSERT INTO sync_state (source, cursor, synced_at, last_error)
                   VALUES (?, ?, ?, NULL)
                   ON CONFLICT(source) DO UPDATE SET
                     cursor = excluded.cursor,
                     synced_at = excluded.synced_at,
                     last_error = NULL""",
                (source, json.dumps(cursor or {}, ensure_ascii=False), utcnow_iso()),
            )
        else:
            conn.execute(
                """INSERT INTO sync_state (source, cursor, synced_at, last_error)
                   VALUES (?, '{}', ?, ?)
                   ON CONFLICT(source) DO UPDATE SET
                     synced_at = excluded.synced_at,
                     last_error = excluded.last_error""",
                (source, utcnow_iso(), error),
            )


def _redact_tree(value):
    """Apply secret redaction to every string in a JSON-ish structure."""
    if isinstance(value, str):
        return redact.redact(value)
    if isinstance(value, list):
        return [_redact_tree(v) for v in value]
    if isinstance(value, dict):
        return {k: _redact_tree(v) for k, v in value.items()}
    return value


def upsert_items(source: str, kind: str, records: list[dict]) -> dict:
    """Diff-import external items (M1: karakeep bookmarks, zotero references).

    Mirrors upsert_conversations: secret redaction happens HERE, at the single
    choke point for external-item ingest, and BEFORE content_hash — so
    re-syncs of unchanged source data hash identically (skip), and a redaction
    pattern update changes the hash and propagates on the next full sweep
    (§6.3: 外部ソース由来テキストにも redaction を適用).

    Each record: {external_id, title, url, doi (raw, optional), created_at,
    updated_at, meta: dict}. url_norm / doi are derived HERE, from the
    already-redacted url — connectors must not pre-compute them. That keeps a
    token in a URL query string out of every column (Codex M1 review, should
    #1) and matches the conversation path's ordering (redact → extract →
    normalise), so both sides of a link derive keys from redacted text.
    The content_hash covers the redacted (title, url, url_norm, doi, meta) —
    timestamps excluded, so a pure dateModified touch with identical content
    stays a skip.

    Returns {"inserted", "updated", "skipped", "changed_ids"}.
    """
    conn = connect()
    stats = {"inserted": 0, "updated": 0, "skipped": 0, "changed_ids": []}
    with conn:
        for rec in records:
            title = redact.redact_title(rec.get("title") or "") or None
            url = redact.redact(rec["url"]) if rec.get("url") else None
            url_norm = urlnorm.normalize_url(url)
            doi_raw = rec.get("doi")
            doi = (
                urlnorm.normalize_doi(redact.redact(doi_raw) if doi_raw else None)
                or urlnorm.normalize_doi(url_norm)
            )
            meta = _redact_tree(rec.get("meta") or {})
            meta_json = json.dumps(meta, ensure_ascii=False, sort_keys=True)
            basis = json.dumps(
                [kind, title, url, url_norm, doi, meta],
                ensure_ascii=False, sort_keys=True,
            )
            new_hash = hashlib.sha256(basis.encode()).hexdigest()
            row = conn.execute(
                "SELECT id, content_hash FROM items WHERE source = ? AND external_id = ?",
                (source, rec["external_id"]),
            ).fetchone()
            if row is not None and row["content_hash"] == new_hash:
                stats["skipped"] += 1
                continue
            cur = conn.execute(
                """INSERT INTO items
                     (kind, source, external_id, title, url, url_norm, doi,
                      created_at, updated_at, content_hash, meta)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(source, external_id) DO UPDATE SET
                     kind = excluded.kind,
                     title = excluded.title,
                     url = excluded.url,
                     url_norm = excluded.url_norm,
                     doi = excluded.doi,
                     created_at = excluded.created_at,
                     updated_at = excluded.updated_at,
                     content_hash = excluded.content_hash,
                     meta = excluded.meta
                   RETURNING id""",
                (kind, source, rec["external_id"], title, url, url_norm, doi,
                 rec.get("created_at"), rec.get("updated_at"), new_hash, meta_json),
            )
            item_id = cur.fetchone()[0]
            stats["inserted" if row is None else "updated"] += 1
            stats["changed_ids"].append(item_id)
    return stats


def prune_items(source: str, *, keep_external_ids: list[str]) -> int:
    """Remove registry entries for a source that no longer exist upstream
    (M3: notes deleted/renamed in the Obsidian vault).

    Only derived data is touched: the items rows, their item_text chunks
    (embeddings CASCADE off chunks; chunks_fts via trigger). Originals —
    conversations/messages — are never candidates because they are not
    external-source items. Returns the number of items removed.
    """
    conn = connect()
    keep = set(keep_external_ids)
    rows = conn.execute(
        "SELECT id, external_id FROM items WHERE source = ?", (source,)
    ).fetchall()
    stale = [r["id"] for r in rows if r["external_id"] not in keep]
    if not stale:
        return 0
    with conn:
        ph = ",".join("?" * len(stale))
        conn.execute(f"DELETE FROM chunks WHERE item_id IN ({ph})", stale)
        conn.execute(
            f"DELETE FROM item_links WHERE a_id IN ({ph}) OR b_id IN ({ph})",
            [*stale, *stale],
        )
        conn.execute(f"DELETE FROM items WHERE id IN ({ph})", stale)
    return len(stale)


# A (via, key) group above this size is treated as boilerplate/noise rather
# than a meaningful cross-source match (see rebuild_item_links docstring).
# Organic same-link sharing in this archive tops out in the low 20s; the
# repeated-auto-tweet groups it excludes start at 70+ — comfortably clear
# of any plausible organic cluster.
_LINK_GROUP_CAP = 30


def rebuild_item_links() -> dict:
    """Full rebuild of item_links from scratch (M1, DESIGN.md §5.1 D5).

    item_links is derived data: dropping and regenerating it is always safe
    (invariant 3), and a full rebuild keeps this idempotent — no stale links
    when an item's url_norm changes. Three key spaces, matched by exact
    equality after normalisation:

    - 'url'    — items.url_norm, plus URLs extracted from conversation
                 message text (§7 M1: 会話本文中の URL 抽出→正規化→突合)
    - 'doi'    — items.doi, plus doi.org URLs found in message text
    - 'github' — repo-level keys derived from the above URLs

    The message scan is a LIKE-prefiltered pass over messages.text on every
    call; at personal-archive scale (~15k messages) this is a couple of
    seconds. If it ever hurts, per-item key caching would need a schema
    addition (DESIGN.md §4 change) — report, don't build it ad hoc.

    A (via, key) group larger than _LINK_GROUP_CAP is skipped entirely
    (found 2026-07-16: X import surfaced old auto-tweet boilerplate —
    paper.li/fllwrs.com/Ustream links repeated 70-595 times — pairing every
    member is a quadratic blow-up and each such group is noise, not a
    meaningful cross-source match; linked_items() has no limit clause, so
    an uncapped group also floods build_context_pack for every member).
    """
    conn = connect()
    key_map: dict[tuple[str, str], set[int]] = {}

    def add(via: str, key: str | None, item_id: int) -> None:
        if key:
            key_map.setdefault((via, key), set()).add(item_id)

    for row in conn.execute(
        "SELECT id, url_norm, doi FROM items WHERE url_norm IS NOT NULL OR doi IS NOT NULL"
    ):
        add("url", row["url_norm"], row["id"])
        add("doi", row["doi"], row["id"])
        if row["url_norm"]:
            add("github", urlnorm.extract_github_repo(row["url_norm"]), row["id"])

    for row in conn.execute(
        """SELECT i.id AS item_id, m.text
           FROM messages m
           JOIN conversations c ON c.id = m.conversation_id
           JOIN items i ON i.source = c.source AND i.external_id = c.source_id
           WHERE m.text LIKE '%http%'"""
    ):
        for raw in urlnorm.extract_urls(row["text"]):
            norm, doi, github = urlnorm.url_keys(raw)
            add("url", norm, row["item_id"])
            add("doi", doi, row["item_id"])
            add("github", github, row["item_id"])

    pairs: set[tuple[int, int, str]] = set()
    for (via, _key), ids in key_map.items():
        if len(ids) < 2 or len(ids) > _LINK_GROUP_CAP:
            continue
        ordered = sorted(ids)
        for idx, a in enumerate(ordered):
            for b in ordered[idx + 1:]:
                pairs.add((a, b, via))

    with conn:
        conn.execute("DELETE FROM item_links")
        conn.executemany(
            "INSERT OR IGNORE INTO item_links (a_id, b_id, link_via) VALUES (?,?,?)",
            sorted(pairs),
        )
    counts = {"url": 0, "doi": 0, "github": 0}
    for _a, _b, via in pairs:
        counts[via] += 1
    counts["total"] = len(pairs)
    return counts


def _store_chunks(
    conn: sqlite3.Connection,
    message_id: int,
    conversation_id: int,
    text: str,
    chunking_version: str,
    item_id: int,
) -> int:
    """Chunk one message's text and insert the rows. Caller owns the transaction.
    Returns the number of chunks stored (0 for empty/whitespace text).

    item_id (v11) is required — every chunk carries its cross-source join key.
    Making it a positional required arg surfaces missing wiring at call sites
    instead of silently writing NULLs that later fail integrity_check."""
    chunks = chunk_text(text)
    if not chunks:
        return 0
    now = utcnow_iso()
    conn.executemany(
        "INSERT INTO chunks"
        " (message_id, conversation_id, idx, start_offset, end_offset, text,"
        "  kind, chunking_version, created_at, item_id)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        [
            (message_id, conversation_id, c.idx, c.start_offset, c.end_offset,
             c.text, "message_text", chunking_version, now, item_id)
            for c in chunks
        ],
    )
    return len(chunks)


def rechunk_messages(
    message_ids: list[int] | None = None,
    *,
    chunking_version: str | None = None,
    force: bool = False,
) -> dict:
    """(Re)generate chunks for messages — the regenerable path for derived data.

    - `message_ids=None` covers every message; otherwise just the listed ids.
    - Messages that already have chunks at `chunking_version` are skipped unless
      `force=True`, in which case their chunks at that version are replaced
      (use after a chunking-algorithm or redaction-rule change).

    Returns {messages, chunks, skipped}: messages (re)chunked, chunks written,
    messages skipped as already current.
    """
    chunking_version = chunking_version or CURRENT_CHUNKING_VERSION
    conn = connect()
    stats = {"messages": 0, "chunks": 0, "skipped": 0}
    # conversation_id -> items.id. Cached across rows so we don't run the upsert
    # once per message; also self-heals a legacy DB where the items row for a
    # conversation is missing (raw-SQL test fixtures, hand-edited state).
    item_id_by_conv: dict[int, int] = {}
    with conn:
        if message_ids is None:
            rows = conn.execute(
                "SELECT id, conversation_id, text FROM messages ORDER BY id"
            ).fetchall()
        else:
            rows = [
                conn.execute(
                    "SELECT id, conversation_id, text FROM messages WHERE id=?", (mid,)
                ).fetchone()
                for mid in message_ids
            ]
            rows = [r for r in rows if r is not None]
        for row in rows:
            has = conn.execute(
                "SELECT COUNT(*) FROM chunks WHERE message_id=? AND chunking_version=?",
                (row["id"], chunking_version),
            ).fetchone()[0]
            if has and not force:
                stats["skipped"] += 1
                continue
            if has:
                conn.execute(
                    "DELETE FROM chunks WHERE message_id=? AND chunking_version=?",
                    (row["id"], chunking_version),
                )
            conv_id = row["conversation_id"]
            if conv_id not in item_id_by_conv:
                item_id_by_conv[conv_id] = _ensure_item_for_conversation(conn, conv_id)
            n = _store_chunks(conn, row["id"], conv_id, row["text"],
                              chunking_version, item_id_by_conv[conv_id])
            stats["messages"] += 1
            stats["chunks"] += n
    return stats


def _item_index_text(title: str | None, meta: dict) -> str:
    """Assemble the indexable text of an external item (M2, DESIGN.md §4).

    Title plus the text-bearing meta fields the connectors store (karakeep:
    description/note/summary/text + tags; zotero: abstract/creators/
    publication + tags). Field order is fixed so the output — and therefore
    chunk offsets — is deterministic across resyncs. Values are already
    redacted (upsert_items choke point)."""
    parts: list[str] = []
    if title:
        parts.append(title)
    for key in ("description", "note", "summary", "text", "abstract", "publication"):
        val = meta.get(key)
        if isinstance(val, str) and val.strip():
            parts.append(val)
    for key in ("creators", "tags"):
        val = meta.get(key)
        if isinstance(val, list) and val:
            parts.append(", ".join(str(v) for v in val))
    return "\n".join(parts)


def rechunk_items(
    item_ids: list[int] | None = None,
    *,
    chunking_version: str | None = None,
    force: bool = False,
) -> dict:
    """(Re)generate kind='item_text' chunks for non-conversation items (M2).

    Mirrors rechunk_messages: skip items already chunked at this version
    unless force=True (connector syncs pass force=True for changed items so
    edited bookmarks re-chunk). Conversation items are excluded — their text
    is indexed through message chunks. chunks_fts stays in sync via triggers.

    Returns {items, chunks, skipped, chunk_ids} — chunk_ids of the freshly
    written chunks, so callers (connector sync) can embed exactly the new
    rows instead of sweeping the whole table.
    """
    chunking_version = chunking_version or CURRENT_CHUNKING_VERSION
    conn = connect()
    stats: dict = {"items": 0, "chunks": 0, "skipped": 0, "chunk_ids": []}
    with conn:
        if item_ids is None:
            rows = conn.execute(
                "SELECT id, title, meta FROM items WHERE kind != 'conversation' ORDER BY id"
            ).fetchall()
        else:
            rows = [
                conn.execute(
                    "SELECT id, title, meta FROM items WHERE id=? AND kind != 'conversation'",
                    (iid,),
                ).fetchone()
                for iid in item_ids
            ]
            rows = [r for r in rows if r is not None]
        now = utcnow_iso()
        for row in rows:
            has = conn.execute(
                "SELECT COUNT(*) FROM chunks WHERE item_id=? AND kind='item_text'"
                " AND chunking_version=?",
                (row["id"], chunking_version),
            ).fetchone()[0]
            if has and not force:
                stats["skipped"] += 1
                continue
            if has:
                conn.execute(
                    "DELETE FROM chunks WHERE item_id=? AND kind='item_text'"
                    " AND chunking_version=?",
                    (row["id"], chunking_version),
                )
            text = _item_index_text(row["title"], json.loads(row["meta"] or "{}"))
            stats["items"] += 1
            for c in chunk_text(text):
                cur = conn.execute(
                    "INSERT INTO chunks"
                    " (message_id, conversation_id, idx, start_offset, end_offset,"
                    "  text, kind, chunking_version, created_at, item_id)"
                    " VALUES (NULL, NULL, ?, ?, ?, ?, 'item_text', ?, ?, ?)",
                    (c.idx, c.start_offset, c.end_offset, c.text,
                     chunking_version, now, row["id"]),
                )
                stats["chunks"] += 1
                stats["chunk_ids"].append(cur.lastrowid)
    return stats


def vector_index() -> VectorIndex:
    """Return the active VectorIndex implementation for this connection.

    SQLiteVecIndex when the extension loaded, else NumpyIndex. The choice is
    per-thread because the load attempt happens in connect(); CAIRN_VECTOR_INDEX=numpy
    forces NumpyIndex regardless. The returned object closes over `connect()`,
    so it always operates on the current thread-local connection."""
    if _sqlite_vec_loaded():
        return SQLiteVecIndex(connect)
    return NumpyIndex(connect)


def embed_chunks(
    provider: EmbeddingProvider,
    *,
    chunk_ids: list[int] | None = None,
    only_missing: bool = True,
    batch_size: int = 32,
) -> dict:
    """Generate embeddings for chunks using `provider`.

    - `chunk_ids=None` covers every chunk; otherwise just the listed ids.
    - `only_missing=True` (default) skips chunks that already have a row for
      this (provider.name, provider.model). Set False to overwrite.
    - `batch_size` is the embed-call batch width (the provider may batch
      internally too; this caps memory + DB-row lots).

    Returns {chunks, skipped}: chunks freshly embedded, chunks skipped as
    already present at this (provider, model).
    """
    conn = connect()
    stats = {"chunks": 0, "skipped": 0}
    # Pick the chunk pool. We fetch text up front because the provider may run
    # for seconds per batch — keeping the SELECT cursor open across that would
    # hold a read transaction longer than needed.
    if chunk_ids is None:
        rows = conn.execute("SELECT id, text FROM chunks ORDER BY id").fetchall()
    else:
        rows = [
            conn.execute("SELECT id, text FROM chunks WHERE id=?", (cid,)).fetchone()
            for cid in chunk_ids
        ]
        rows = [r for r in rows if r is not None]

    if only_missing:
        # Filter to chunks with no row for this provider+model. A single SQL
        # check per chunk keeps the logic simple; the index on
        # embeddings(chunk_id) keeps it cheap.
        kept = []
        for row in rows:
            has = conn.execute(
                "SELECT 1 FROM embeddings WHERE chunk_id=? AND provider=? AND model=? LIMIT 1",
                (row["id"], provider.name, provider.model),
            ).fetchone()
            if has:
                stats["skipped"] += 1
            else:
                kept.append(row)
        rows = kept

    dimension = provider.dimension
    now = utcnow_iso()
    idx = vector_index()
    for i in range(0, len(rows), batch_size):
        batch = rows[i:i + batch_size]
        vectors = provider.embed_passages([r["text"] for r in batch])
        with conn:
            # INSERT OR REPLACE so only_missing=False (force-reembed) updates
            # in place via the UNIQUE(chunk_id, provider, model) key.
            conn.executemany(
                "INSERT OR REPLACE INTO embeddings"
                " (chunk_id, provider, model, dimension, vector, created_at)"
                " VALUES (?,?,?,?,?,?)",
                [
                    (r["id"], provider.name, provider.model, dimension, v, now)
                    for r, v in zip(batch, vectors)
                ],
            )
            # Mirror to the vector index so KNN stays current. For NumpyIndex
            # this is a no-op (it reads from the embeddings table directly);
            # for SQLiteVecIndex it keeps the vec0 mirror in sync.
            for r, v in zip(batch, vectors):
                idx.upsert(r["id"], v, dimension)
        stats["chunks"] += len(batch)
    return stats


def find_similar_chunks(
    query_vector: bytes,
    *,
    provider: str,
    model: str,
    k: int = 10,
    source: str | None = None,
    kinds: list[str] | None = None,
    after: str | None = None,
    before: str | None = None,
) -> list[dict]:
    """Top-k chunks by cosine similarity for one (provider, model).

    Two-stage pipeline: filter candidates via SQL (provider/model + optional
    source/kind/date), then delegate KNN to the active VectorIndex
    (sqlite-vec when available, NumpyIndex otherwise). Since M2 the filter
    joins `items` — the cross-source pivot — so external-item chunks and
    conversation chunks compete in one candidate pool. items.updated_at
    mirrors conversations.updated_at for conversation items, so date filters
    behave as before.
    """
    conn = connect()
    # Stage 1: candidate chunk_ids for this (provider, model) + filters.
    # The dimension check excludes corrupted / cross-model embeddings whose
    # vector length disagrees with the query — important because the vector
    # index may hold an independent copy that could otherwise be matched.
    query_dim = len(query_vector) // 4
    sql = (
        "SELECT e.chunk_id FROM embeddings e "
        "JOIN chunks ch ON ch.id = e.chunk_id "
        "JOIN items i ON i.id = ch.item_id "
        "WHERE e.provider=? AND e.model=? AND e.dimension=?"
    )
    params: list = [provider, model, query_dim]
    if source:
        sql += " AND i.source=?"; params.append(source)
    if kinds:
        sql += f" AND i.kind IN ({','.join('?' * len(kinds))})"; params.extend(kinds)
    if after:
        sql += " AND i.updated_at >= ?"; params.append(after)
    if before:
        sql += " AND i.updated_at <= ?"; params.append(before)
    candidates = [r[0] for r in conn.execute(sql, params).fetchall()]
    if not candidates:
        return []

    # Stage 2: KNN. Prefer the configured index; if it returns nothing
    # (sqlite-vec dim mismatch, vec0 not yet populated), fall back to
    # NumpyIndex against the same embeddings table.
    idx = vector_index()
    pairs = idx.search(query_vector, k, candidates=candidates)
    if not pairs and idx.name != "numpy":
        pairs = NumpyIndex(connect).search(query_vector, k, candidates=candidates)
    if not pairs:
        return []

    # Stage 3: hydrate top-k with chunk text and item metadata.
    chunk_ids = [c for c, _ in pairs]
    placeholders = ",".join("?" * len(chunk_ids))
    rows = conn.execute(
        f"SELECT ch.id, ch.message_id, ch.conversation_id, ch.text, "
        f"       i.id AS item_id, i.kind, i.source, i.title, i.url, "
        f"       i.external_id, i.updated_at "
        f"FROM chunks ch JOIN items i ON i.id = ch.item_id "
        f"WHERE ch.id IN ({placeholders})",
        chunk_ids,
    ).fetchall()
    by_id = {r["id"]: r for r in rows}
    return [
        {
            "chunk_id": cid,
            "message_id": by_id[cid]["message_id"],
            "conversation_id": by_id[cid]["conversation_id"],
            "item_id": by_id[cid]["item_id"],
            "kind": by_id[cid]["kind"],
            "title": by_id[cid]["title"],
            "url": by_id[cid]["url"],
            "external_id": by_id[cid]["external_id"],
            "score": score,
            "text": by_id[cid]["text"],
            "source": by_id[cid]["source"],
            "updated_at": by_id[cid]["updated_at"],
        }
        for cid, score in pairs
        if cid in by_id
    ]


def record_import_run(
    *,
    source: str,
    input_name: str | None,
    started_at: str,
    completed_at: str | None = None,
    parser_version: str | None = None,
    inserted: int = 0,
    updated: int = 0,
    skipped: int = 0,
    failed: int = 0,
    conversations: int = 0,
    warnings: list[str] | None = None,
    content_hash: str | None = None,
    status: str = "ok",
    error: str | None = None,
) -> int:
    """Record one import of a single input. Returns the new run's id."""
    warnings = warnings or []
    summary = "\n".join(warnings)[:2000] if warnings else None
    conn = connect()
    with conn:
        cur = conn.execute(
            """INSERT INTO import_runs
               (source, input_name, started_at, completed_at, parser_version,
                inserted, updated, skipped, failed, conversations,
                warnings, warning_summary, content_hash, status, error)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (source, input_name, started_at, completed_at, parser_version,
             inserted, updated, skipped, failed, conversations,
             len(warnings), summary, content_hash, status, error),
        )
    return cur.lastrowid


def list_import_runs(
    limit: int = 50, offset: int = 0, source: str | None = None
) -> list[dict]:
    conn = connect()
    conds, params = [], []
    if source:
        conds.append("source = ?")
        params.append(source)
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    params += [limit, offset]
    rows = conn.execute(
        f"SELECT * FROM import_runs {where} ORDER BY id DESC LIMIT ? OFFSET ?",
        params,
    ).fetchall()
    return [dict(r) for r in rows]


def start_extraction_run(
    *,
    kind: str,
    scope: str,
    provider: str,
    model: str | None,
    prompt_version: str,
    started_at: str,
) -> int:
    """Insert a running extraction_run and return its id."""
    conn = connect()
    with conn:
        cur = conn.execute(
            """INSERT INTO extraction_runs
               (kind, scope, provider, model, prompt_version, started_at, status)
               VALUES (?,?,?,?,?,?,'running')""",
            (kind, scope, provider, model, prompt_version, started_at),
        )
    return cur.lastrowid


def finish_extraction_run(
    run_id: int,
    *,
    completed_at: str,
    status: str = "ok",
    input_token_count: int | None = None,
    output_token_count: int | None = None,
    retries: int = 0,
    warnings: list[str] | None = None,
    error: str | None = None,
) -> None:
    """Update an extraction_run row when the batch completes."""
    warnings = warnings or []
    summary = "\n".join(warnings)[:2000] if warnings else None
    conn = connect()
    with conn:
        conn.execute(
            """UPDATE extraction_runs
               SET completed_at=?, status=?, input_token_count=?,
                   output_token_count=?, retries=?, warnings=?,
                   warning_summary=?, error=?
               WHERE id=?""",
            (completed_at, status, input_token_count, output_token_count,
             retries, len(warnings), summary, error, run_id),
        )


def list_extraction_runs(
    limit: int = 50, offset: int = 0, kind: str | None = None
) -> list[dict]:
    conn = connect()
    conds, params = [], []
    if kind:
        conds.append("kind = ?")
        params.append(kind)
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    params += [limit, offset]
    rows = conn.execute(
        f"SELECT * FROM extraction_runs {where} ORDER BY id DESC LIMIT ? OFFSET ?",
        params,
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Entities (P3-B)
# ---------------------------------------------------------------------------

def upsert_entity(
    *,
    kind: str,
    canonical_name: str,
    external_id: str | None = None,
    meta: str = "{}",
    created_at: str,
) -> int:
    """Insert entity if not exists; return the id in both cases."""
    conn = connect()
    with conn:
        conn.execute(
            """INSERT INTO entities (kind, canonical_name, external_id, meta, created_at)
               VALUES (?,?,?,?,?)
               ON CONFLICT(kind, canonical_name) DO NOTHING""",
            (kind, canonical_name, external_id, meta, created_at),
        )
    row = conn.execute(
        "SELECT id FROM entities WHERE kind=? AND canonical_name=?",
        (kind, canonical_name),
    ).fetchone()
    return row["id"]


def upsert_entity_mention(
    *,
    entity_id: int,
    message_id: int,
    conversation_id: int,
    start_offset: int,
    end_offset: int,
    surface: str,
    detector: str,
    created_at: str,
) -> None:
    """Insert entity mention if not exists; silently ignore duplicates."""
    conn = connect()
    with conn:
        conn.execute(
            """INSERT INTO entity_mentions
               (entity_id, message_id, conversation_id,
                start_offset, end_offset, surface, detector, created_at)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(entity_id, message_id, start_offset) DO NOTHING""",
            (entity_id, message_id, conversation_id,
             start_offset, end_offset, surface, detector, created_at),
        )


def count_entities(kind: str | None = None) -> int:
    conn = connect()
    if kind:
        return conn.execute("SELECT COUNT(*) FROM entities WHERE kind=?", (kind,)).fetchone()[0]
    return conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]


def count_entity_mentions(detector: str | None = None) -> int:
    conn = connect()
    if detector:
        return conn.execute(
            "SELECT COUNT(*) FROM entity_mentions WHERE detector=?", (detector,)
        ).fetchone()[0]
    return conn.execute("SELECT COUNT(*) FROM entity_mentions").fetchone()[0]


def orphan_entity_mentions() -> list[dict]:
    """Return entity_mentions rows whose entity_id has no matching entity (integrity check)."""
    conn = connect()
    rows = conn.execute(
        """SELECT em.id, em.entity_id, em.message_id, em.detector
           FROM entity_mentions em
           LEFT JOIN entities e ON e.id = em.entity_id
           WHERE e.id IS NULL""",
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Segments (P3-C)
# ---------------------------------------------------------------------------

def insert_segment(
    *,
    conversation_id: int,
    idx: int,
    start_message_id: int,
    end_message_id: int,
    title: str,
    summary: str,
    topics: str = "[]",
    generated_by: str,
    prompt_version: str,
    extraction_run_id: int | None = None,
    created_at: str,
) -> int:
    """Insert a segment row and return its id. Caller must handle UNIQUE conflict."""
    conn = connect()
    with conn:
        cur = conn.execute(
            """INSERT INTO segments
               (conversation_id, idx, start_message_id, end_message_id,
                title, summary, topics, generated_by, prompt_version,
                extraction_run_id, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (conversation_id, idx, start_message_id, end_message_id,
             title, summary, topics, generated_by, prompt_version,
             extraction_run_id, created_at),
        )
    return cur.lastrowid


def delete_unlocked_segments(conversation_id: int) -> int:
    """Delete non-locked segments for a conversation; return count deleted."""
    conn = connect()
    with conn:
        cur = conn.execute(
            "DELETE FROM segments WHERE conversation_id=? AND locked_by_user=0",
            (conversation_id,),
        )
    return cur.rowcount


def list_segments(
    conversation_id: int | None = None,
    limit: int = 200,
    offset: int = 0,
) -> list[dict]:
    conn = connect()
    if conversation_id is not None:
        rows = conn.execute(
            "SELECT * FROM segments WHERE conversation_id=? ORDER BY idx LIMIT ? OFFSET ?",
            (conversation_id, limit, offset),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM segments ORDER BY conversation_id, idx LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
    return [dict(r) for r in rows]


def get_message_ids_for_conversation(conversation_id: int) -> list[int]:
    """Return ordered list of message ids for a conversation."""
    conn = connect()
    rows = conn.execute(
        "SELECT id FROM messages WHERE conversation_id=? ORDER BY id",
        (conversation_id,),
    ).fetchall()
    return [r["id"] for r in rows]


def conversations_without_segments(
    since: str | None = None, limit: int | None = None
) -> list[dict]:
    """Return conversations that have no segment rows yet."""
    conn = connect()
    conds = ["NOT EXISTS (SELECT 1 FROM segments s WHERE s.conversation_id = c.id)"]
    params: list = []
    if since:
        conds.append("c.updated_at >= ?")
        params.append(since)
    where = "WHERE " + " AND ".join(conds)
    q = f"SELECT c.id, c.source, c.title FROM conversations c {where} ORDER BY c.id"
    if limit:
        q += f" LIMIT {int(limit)}"
    rows = conn.execute(q, params).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Assertions (P3-D)
# ---------------------------------------------------------------------------

def insert_assertion(
    *,
    segment_id: int,
    conversation_id: int,
    text: str,
    actor: str,
    kind: str,
    status: str = "tentative",
    confidence: float | None = None,
    supporting_message_ids: str = "[]",
    generated_by: str,
    prompt_version: str,
    extraction_run_id: int | None = None,
    created_at: str,
) -> int:
    conn = connect()
    with conn:
        cur = conn.execute(
            """INSERT INTO assertions
               (segment_id, conversation_id, text, actor, kind, status,
                confidence, supporting_message_ids, generated_by,
                prompt_version, extraction_run_id, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (segment_id, conversation_id, text, actor, kind, status,
             confidence, supporting_message_ids, generated_by,
             prompt_version, extraction_run_id, created_at),
        )
    return cur.lastrowid


def delete_unlocked_assertions(segment_id: int) -> int:
    conn = connect()
    with conn:
        cur = conn.execute(
            "DELETE FROM assertions WHERE segment_id=? AND locked_by_user=0",
            (segment_id,),
        )
    return cur.rowcount


def list_assertions(
    segment_id: int | None = None,
    conversation_id: int | None = None,
    limit: int = 500,
    offset: int = 0,
) -> list[dict]:
    conn = connect()
    conds, params = [], []
    if segment_id is not None:
        conds.append("segment_id = ?")
        params.append(segment_id)
    if conversation_id is not None:
        conds.append("conversation_id = ?")
        params.append(conversation_id)
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    params += [limit, offset]
    rows = conn.execute(
        f"SELECT * FROM assertions {where} ORDER BY segment_id, id LIMIT ? OFFSET ?",
        params,
    ).fetchall()
    return [dict(r) for r in rows]


def segments_without_assertions(
    since: str | None = None, limit: int | None = None
) -> list[dict]:
    """Return segments that have no assertion rows yet."""
    conn = connect()
    conds = ["NOT EXISTS (SELECT 1 FROM assertions a WHERE a.segment_id = s.id)"]
    params: list = []
    if since:
        conds.append("s.created_at >= ?")
        params.append(since)
    where = "WHERE " + " AND ".join(conds)
    q = f"SELECT s.id, s.conversation_id, s.idx FROM segments s {where} ORDER BY s.id"
    if limit:
        q += f" LIMIT {int(limit)}"
    rows = conn.execute(q, params).fetchall()
    return [dict(r) for r in rows]


def _fts_query(q: str) -> str:
    """Escape user input as quoted phrases (AND-joined by whitespace)."""
    terms = [t for t in q.split() if t]
    return " AND ".join('"' + t.replace('"', '""') + '"' for t in terms)


def _make_snippet(text: str, q: str, width: int = 80) -> str:
    pos = text.lower().find(q.lower())
    if pos < 0:
        return text[:width * 2]
    start = max(0, pos - width)
    end = min(len(text), pos + len(q) + width)
    return ("…" if start > 0 else "") + text[start:end] + ("…" if end < len(text) else "")


def search(
    q: str,
    *,
    mode: str = "keyword",
    provider: EmbeddingProvider | None = None,
    source: str | None = None,
    kinds: list[str] | None = None,
    limit: int = 50,
    offset: int = 0,
    after: str | None = None,
    before: str | None = None,
) -> list[dict]:
    """Cross-source search; one row per item (best hit per item).

    Since M2 the result space is the items registry: conversations (via
    messages_fts) and external items — bookmarks, references, notes — (via
    chunks_fts) compete in one ranked list. `kinds` narrows by items.kind;
    None means everything. Archives without external items get byte-identical
    results to the pre-M2 conversation-only behaviour.

    Modes (kw-only):
      "keyword"  — FTS5 trigram (or LIKE fallback for <3-char terms). Free.
                   Conversation and item hit lists are fused with RRF when
                   both are non-empty (bm25 scores from two FTS tables are
                   not directly comparable; ranks are).
      "semantic" — embed q, KNN against `embeddings` for the active (provider,
                   model), aggregate to best chunk per item.
      "hybrid"   — keyword + semantic, fused with RRF (k₀=60), keyed by item.

    Default is "keyword" to keep existing callers byte-compatible and to avoid
    silently loading an embedding model on every legacy /api/search call.
    UIs that want semantic by default should pass mode="hybrid" explicitly.

    Result fields added in M2: kind, item_id, url, external_id
    (conversation rows keep conversation_id / role / message_id; external
    rows carry None there).
    """
    if mode == "keyword":
        return _search_keyword(q, source=source, kinds=kinds, limit=limit,
                               offset=offset, after=after, before=before)
    if mode in ("semantic", "hybrid"):
        if provider is None:
            provider = _active_embedding_provider()
        if mode == "semantic":
            return _search_semantic(q, provider=provider, source=source,
                                    kinds=kinds, limit=limit, offset=offset,
                                    after=after, before=before)
        return _search_hybrid(q, provider=provider, source=source,
                              kinds=kinds, limit=limit, offset=offset,
                              after=after, before=before)
    raise ValueError(f"unknown search mode: {mode!r}")


def _search_keyword(
    q: str,
    *,
    source: str | None,
    kinds: list[str] | None = None,
    limit: int,
    offset: int,
    after: str | None,
    before: str | None,
) -> list[dict]:
    """Keyword path. Conversation hits (messages_fts, Phase-1 logic
    preserved) and external-item hits (chunks_fts, M2) are computed as two
    ranked lists. When only one list has hits the legacy SQL paging applies
    (conversation-only archives stay byte-compatible); when both do, the
    lists are RRF-fused and paged in Python. Results get
    match_reason="keyword" and matched_keywords parsed from the snippet."""
    terms = [t for t in q.split() if t]
    if not terms:
        return []
    use_fts = all(len(t) >= 3 for t in terms)

    want_conv = kinds is None or "conversation" in kinds
    ext_kinds = None if kinds is None else [k for k in kinds if k != "conversation"]
    want_items = kinds is None or bool(ext_kinds)

    item_rows = (
        _keyword_item_rows(q, terms, use_fts, source=source, kinds=ext_kinds,
                           after=after, before=before, fetch=limit + offset)
        if want_items else []
    )
    if not want_conv:
        return item_rows[offset:offset + limit]
    if not item_rows:
        # conversation-only outcome: keep the pre-M2 SQL paging path verbatim
        return _keyword_conv_rows(q, terms, use_fts, source=source,
                                  after=after, before=before,
                                  limit=limit, offset=offset)
    conv_rows = _keyword_conv_rows(q, terms, use_fts, source=source,
                                   after=after, before=before,
                                   limit=limit + offset, offset=0)
    if use_fts:
        merged = _rrf_merge([conv_rows, item_rows], key=lambda r: r["item_id"])
    else:
        # LIKE fallback has no rank; both lists are recency-ordered already
        merged = sorted(conv_rows + item_rows,
                        key=lambda r: r["updated_at"] or "", reverse=True)
    return merged[offset:offset + limit]


def _rrf_merge(lists: list[list[dict]], *, key) -> list[dict]:
    """Fuse pre-ranked result lists with Reciprocal Rank Fusion. When the
    same key appears in several lists the first list's row wins (callers
    order lists by row preference)."""
    scores: dict = {}
    rows: dict = {}
    for lst in lists:
        for pos, row in enumerate(lst):
            k = key(row)
            scores[k] = scores.get(k, 0.0) + 1.0 / (_RRF_K + pos + 1)
            rows.setdefault(k, row)
    return [rows[k] for k in sorted(scores, key=lambda k: scores[k], reverse=True)]


def _keyword_conv_rows(
    q: str,
    terms: list[str],
    use_fts: bool,
    *,
    source: str | None,
    after: str | None,
    before: str | None,
    limit: int,
    offset: int,
) -> list[dict]:
    """Conversation-side keyword hits (messages_fts / LIKE), one row per
    conversation. items is joined only to attach the cross-source keys the
    M2 result shape carries (item_id / kind)."""
    conn = connect()
    src_clause = ""
    src_param: list[str] = []
    if source:
        src_clause += " AND c.source = ? "
        src_param.append(source)
    if after:
        src_clause += " AND c.updated_at >= ? "
        src_param.append(after)
    if before:
        src_clause += " AND c.updated_at <= ? "
        src_param.append(before)

    if use_fts:
        # snippet()/bm25() must live in the plain FTS query; window functions
        # and extra joins go in the middle layer; paging happens in SQL
        # (rn=1 keeps only the best-ranked hit per conversation).
        rows = conn.execute(
            f"""
            SELECT * FROM (
                SELECT c.id AS conversation_id, c.source, c.title, c.created_at, c.updated_at, c.meta,
                       c.source_id AS external_id, i.id AS item_id,
                       m.id AS message_id, m.role, m.created_at AS msg_created_at,
                       hits.snip,
                       COUNT(*) OVER (PARTITION BY c.id) AS hit_count,
                       ROW_NUMBER() OVER (PARTITION BY c.id ORDER BY hits.rank) AS rn,
                       hits.rank AS rank
                FROM (
                    SELECT rowid,
                           snippet(messages_fts, 0, '[[', ']]', '…', 24) AS snip,
                           bm25(messages_fts) AS rank
                    FROM messages_fts
                    WHERE messages_fts MATCH ?
                ) AS hits
                JOIN messages m ON m.id = hits.rowid
                JOIN conversations c ON c.id = m.conversation_id
                LEFT JOIN items i ON i.source = c.source AND i.external_id = c.source_id
                WHERE 1=1 {src_clause}
            )
            WHERE rn = 1
            ORDER BY rank
            LIMIT ? OFFSET ?
            """,
            [_fts_query(q), *src_param, limit, offset],
        ).fetchall()
    else:
        like_clauses = " AND ".join(["m.text LIKE ? ESCAPE '\\'"] * len(terms))
        like_params = [
            "%" + t.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_") + "%"
            for t in terms
        ]
        rows = conn.execute(
            f"""
            SELECT * FROM (
                SELECT c.id AS conversation_id, c.source, c.title, c.created_at, c.updated_at, c.meta,
                       c.source_id AS external_id, i.id AS item_id,
                       m.id AS message_id, m.role, m.created_at AS msg_created_at,
                       m.text AS snip,
                       COUNT(*) OVER (PARTITION BY c.id) AS hit_count,
                       ROW_NUMBER() OVER (PARTITION BY c.id ORDER BY m.idx) AS rn
                FROM messages m
                JOIN conversations c ON c.id = m.conversation_id
                LEFT JOIN items i ON i.source = c.source AND i.external_id = c.source_id
                WHERE {like_clauses} {src_clause}
            )
            WHERE rn = 1
            ORDER BY updated_at DESC
            LIMIT ? OFFSET ?
            """,
            [*like_params, *src_param, limit, offset],
        ).fetchall()

    results = []
    for r in rows:
        snip = r["snip"]
        if not use_fts:
            snip = _make_snippet(snip, terms[0])
            for t in terms:
                snip = re.sub(re.escape(t), lambda m: f"[[{m.group(0)}]]", snip, flags=re.IGNORECASE)
        results.append({
            "conversation_id": r["conversation_id"],
            "item_id": r["item_id"],
            "kind": "conversation",
            "source": r["source"],
            "title": r["title"],
            "url": None,
            "external_id": r["external_id"],
            "updated_at": r["updated_at"] or r["created_at"],
            "meta": json.loads(r["meta"]),
            "snippet": snip,
            "role": r["role"],
            "message_id": r["message_id"],
            "hit_count": r["hit_count"],
            "match_reason": "keyword",
            "matched_keywords": _extract_highlighted(snip),
            "semantic_score": None,
        })
    return results


def _keyword_item_rows(
    q: str,
    terms: list[str],
    use_fts: bool,
    *,
    source: str | None,
    kinds: list[str] | None,
    after: str | None,
    before: str | None,
    fetch: int,
) -> list[dict]:
    """External-item keyword hits (chunks_fts / LIKE over item_text chunks),
    one row per item, shaped like conversation results (conversation-only
    fields carry None)."""
    conn = connect()
    filt = ""
    params: list = []
    if source:
        filt += " AND i.source = ? "
        params.append(source)
    if kinds:
        filt += f" AND i.kind IN ({','.join('?' * len(kinds))}) "
        params.extend(kinds)
    if after:
        filt += " AND i.updated_at >= ? "
        params.append(after)
    if before:
        filt += " AND i.updated_at <= ? "
        params.append(before)

    if use_fts:
        rows = conn.execute(
            f"""
            SELECT * FROM (
                SELECT i.id AS item_id, i.kind, i.source, i.title, i.url,
                       i.external_id, i.created_at, i.updated_at, i.meta,
                       hits.snip,
                       COUNT(*) OVER (PARTITION BY i.id) AS hit_count,
                       ROW_NUMBER() OVER (PARTITION BY i.id ORDER BY hits.rank) AS rn,
                       hits.rank AS rank
                FROM (
                    SELECT rowid,
                           snippet(chunks_fts, 0, '[[', ']]', '…', 24) AS snip,
                           bm25(chunks_fts) AS rank
                    FROM chunks_fts
                    WHERE chunks_fts MATCH ?
                ) AS hits
                JOIN chunks ch ON ch.id = hits.rowid
                JOIN items i ON i.id = ch.item_id
                WHERE 1=1 {filt}
            )
            WHERE rn = 1
            ORDER BY rank
            LIMIT ?
            """,
            [_fts_query(q), *params, fetch],
        ).fetchall()
    else:
        like_clauses = " AND ".join(["ch.text LIKE ? ESCAPE '\\'"] * len(terms))
        like_params = [
            "%" + t.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_") + "%"
            for t in terms
        ]
        rows = conn.execute(
            f"""
            SELECT * FROM (
                SELECT i.id AS item_id, i.kind, i.source, i.title, i.url,
                       i.external_id, i.created_at, i.updated_at, i.meta,
                       ch.text AS snip,
                       COUNT(*) OVER (PARTITION BY i.id) AS hit_count,
                       ROW_NUMBER() OVER (PARTITION BY i.id ORDER BY ch.idx) AS rn
                FROM chunks ch
                JOIN items i ON i.id = ch.item_id
                WHERE ch.kind = 'item_text' AND {like_clauses} {filt}
            )
            WHERE rn = 1
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            [*like_params, *params, fetch],
        ).fetchall()

    results = []
    for r in rows:
        snip = r["snip"]
        if not use_fts:
            snip = _make_snippet(snip, terms[0])
            for t in terms:
                snip = re.sub(re.escape(t), lambda m: f"[[{m.group(0)}]]", snip, flags=re.IGNORECASE)
        results.append({
            "conversation_id": None,
            "item_id": r["item_id"],
            "kind": r["kind"],
            "source": r["source"],
            "title": r["title"],
            "url": _safe_external_url(r["url"]),
            "external_id": r["external_id"],
            "updated_at": r["updated_at"] or r["created_at"],
            "meta": json.loads(r["meta"] or "{}"),
            "snippet": snip,
            "role": None,
            "message_id": None,
            "hit_count": r["hit_count"],
            "match_reason": "keyword",
            "matched_keywords": _extract_highlighted(snip),
            "semantic_score": None,
        })
    return results


def _safe_external_url(url: str | None) -> str | None:
    """Gate external-item URLs to http(s) before they reach search results.

    items.url keeps the redacted raw value (any scheme) for provenance, but
    search consumers treat url as an ACTION target (window.open in the UI,
    link-out in future MCP responses) — a javascript:/data: URL saved in a
    malicious bookmark must not flow there (Codex M2 review, should #1)."""
    if url and re.match(r"^https?://", url, re.IGNORECASE):
        return url
    return None


def _extract_highlighted(snippet: str) -> list[str]:
    """Pull the [[…]] highlights out of a snippet, deduped in order of first
    appearance. Used to surface 'why did this hit?' to the UI without making
    it re-derive terms from the raw query."""
    seen: set[str] = set()
    out: list[str] = []
    for m in re.findall(r"\[\[(.+?)\]\]", snippet):
        if m not in seen:
            seen.add(m)
            out.append(m)
    return out


# --- Phase 2 semantic / hybrid search --------------------------------------

# RRF constant from Cormack et al. 2009. Conventionally 60; higher dampens
# the gap between top and tail, lower sharpens it. 60 is fine for personal-
# archive sizes — the merged list isn't large enough for the choice to matter.
_RRF_K = 60


def _active_embedding_provider() -> EmbeddingProvider:
    """Resolve the (provider, model) the semantic search should use.

    Resolution order:
      1. CAIRN_EMBED_PROVIDER env var, format "name:model"
      2. Most-common (provider, model) in the embeddings table
      3. Fail loudly — embeddings haven't been generated yet

    Step 2 lets a single-model archive Just Work without configuration; step 1
    is the override for A/B or multi-model archives where the latest reindex
    isn't the one the user wants to search against.
    """
    spec = os.environ.get("CAIRN_EMBED_PROVIDER")
    if spec:
        name, _, model = spec.partition(":")
        if not name or not model:
            raise ValueError(
                f"CAIRN_EMBED_PROVIDER must be 'name:model', got {spec!r}"
            )
    else:
        conn = connect()
        row = conn.execute(
            "SELECT provider, model FROM embeddings "
            "GROUP BY provider, model ORDER BY COUNT(*) DESC LIMIT 1"
        ).fetchone()
        if not row:
            raise RuntimeError(
                "no embeddings exist; run `admin reindex` first or set "
                "CAIRN_EMBED_PROVIDER=name:model"
            )
        name, model = row["provider"], row["model"]
    if name == "local-sbert":
        from .embedding.local_sbert import LocalSbertProvider
        return LocalSbertProvider(model=model)
    raise ValueError(
        f"unknown provider {name!r} (known: local-sbert). "
        "Set CAIRN_EMBED_PROVIDER or extend _active_embedding_provider()."
    )


def _search_semantic(
    q: str,
    *,
    provider: EmbeddingProvider,
    source: str | None,
    kinds: list[str] | None = None,
    limit: int,
    offset: int,
    after: str | None,
    before: str | None,
) -> list[dict]:
    """Semantic path: embed → KNN → aggregate to best chunk per item (M2:
    conversations and external items compete in one pool).

    Over-fetches KNN candidates (limit*3 + offset + 50) so the per-item
    deduplication still leaves enough hits to fill the requested page."""
    if not q.strip():
        return []
    query_vector = provider.embed_query(q)
    k = max(limit * 3 + offset + 50, 50)
    hits = find_similar_chunks(
        query_vector,
        provider=provider.name, model=provider.model, k=k,
        source=source, kinds=kinds, after=after, before=before,
    )
    if not hits:
        return []
    # One row per item: best chunk wins; hit_count counts the chunks that
    # matched (so the UI can show "matched in N chunks").
    by_item: dict[int, dict] = {}
    counts: dict[int, int] = {}
    for h in hits:
        iid = h["item_id"]
        counts[iid] = counts.get(iid, 0) + 1
        if iid not in by_item or h["score"] > by_item[iid]["score"]:
            by_item[iid] = h
    ranked = sorted(by_item.values(), key=lambda x: x["score"], reverse=True)
    page = ranked[offset:offset + limit]
    if not page:
        return []
    return _hydrate_semantic(page, counts)


def _hydrate_semantic(hits: list[dict], counts: dict[int, int]) -> list[dict]:
    """Add title/meta/role to KNN hits and shape them like keyword results.

    Conversation hits are hydrated from conversations/messages (items.meta
    mirrors conversations.meta but redact-apply can drift it — the original
    tables are authoritative). External hits hydrate from items."""
    conn = connect()
    conv_hits = [h for h in hits if h["kind"] == "conversation"]
    cids = list({h["conversation_id"] for h in conv_hits})
    mids = list({h["message_id"] for h in conv_hits})
    conv = {r["id"]: r for r in conn.execute(
        f"SELECT id, title, meta, created_at FROM conversations"
        f" WHERE id IN ({','.join('?' * len(cids))})", cids,
    ).fetchall()} if cids else {}
    msg = {r["id"]: r for r in conn.execute(
        f"SELECT id, role FROM messages WHERE id IN ({','.join('?' * len(mids))})",
        mids,
    ).fetchall()} if mids else {}
    item_meta = {r["id"]: r for r in conn.execute(
        f"SELECT id, meta, created_at FROM items"
        f" WHERE id IN ({','.join('?' * len(hits))})",
        [h["item_id"] for h in hits],
    ).fetchall()}
    out: list[dict] = []
    for h in hits:
        snip = h["text"]
        if len(snip) > 200:
            snip = snip[:200] + "…"
        base = {
            "conversation_id": h["conversation_id"],
            "item_id": h["item_id"],
            "kind": h["kind"],
            "source": h["source"],
            "url": _safe_external_url(h["url"]),
            "external_id": h["external_id"],
            "snippet": snip,
            "message_id": h["message_id"],
            "hit_count": counts.get(h["item_id"], 1),
            "match_reason": "semantic",
            "matched_keywords": [],
            "semantic_score": h["score"],
        }
        if h["kind"] == "conversation":
            cm = conv.get(h["conversation_id"])
            mm = msg.get(h["message_id"])
            if not cm or not mm:
                # CASCADE deleted between the KNN call and hydration; skip
                # rather than fabricating a result with missing fields.
                continue
            base.update({
                "title": cm["title"],
                "updated_at": h["updated_at"] or cm["created_at"],
                "meta": json.loads(cm["meta"]),
                "role": mm["role"],
            })
        else:
            im = item_meta.get(h["item_id"])
            if im is None:
                continue
            base.update({
                "title": h["title"],
                "updated_at": h["updated_at"] or im["created_at"],
                "meta": json.loads(im["meta"] or "{}"),
                "role": None,
            })
        out.append(base)
    return out


def _search_hybrid(
    q: str,
    *,
    provider: EmbeddingProvider,
    source: str | None,
    kinds: list[str] | None = None,
    limit: int,
    offset: int,
    after: str | None,
    before: str | None,
) -> list[dict]:
    """Hybrid path: RRF-fuse keyword and semantic rank lists, keyed by item
    (M2: conversations and external items are one result space).

    Pages internally with a generous limit (limit*5, min 100) so the top of
    the merged list is stable even when keyword and semantic agree on few
    items. Pagination applies AFTER merging."""
    internal = max(limit * 5, 100)
    kw = _search_keyword(q, source=source, kinds=kinds, limit=internal,
                         offset=0, after=after, before=before)
    sem = _search_semantic(q, provider=provider, source=source, kinds=kinds,
                           limit=internal, offset=0,
                           after=after, before=before)
    kw_rank = {r["item_id"]: i for i, r in enumerate(kw)}
    sem_rank = {r["item_id"]: i for i, r in enumerate(sem)}
    by_kw = {r["item_id"]: r for r in kw}
    by_sem = {r["item_id"]: r for r in sem}
    all_ids = set(kw_rank) | set(sem_rank)
    if not all_ids:
        return []
    rrf: dict[int, float] = {}
    for iid in all_ids:
        s = 0.0
        if iid in kw_rank:
            s += 1.0 / (_RRF_K + kw_rank[iid] + 1)
        if iid in sem_rank:
            s += 1.0 / (_RRF_K + sem_rank[iid] + 1)
        rrf[iid] = s
    ranked = sorted(all_ids, key=lambda i: rrf[i], reverse=True)
    page = ranked[offset:offset + limit]
    out: list[dict] = []
    for iid in page:
        # Prefer the keyword row when present: its FTS snippet has [[…]]
        # highlights the UI needs; the semantic row's snippet is just the
        # chunk text. Pull semantic_score off the semantic row when there.
        kr = by_kw.get(iid)
        sr = by_sem.get(iid)
        base = dict(kr) if kr else dict(sr)
        if kr and sr:
            base["match_reason"] = "both"
            base["semantic_score"] = sr["semantic_score"]
        # else: base already carries the correct match_reason / semantic_score
        out.append(base)
    return out


def get_conversation(conv_id: int) -> dict | None:
    conn = connect()
    conv = conn.execute("SELECT * FROM conversations WHERE id=?", (conv_id,)).fetchone()
    if not conv:
        return None
    msgs = conn.execute(
        "SELECT id, idx, role, text, created_at, source_message_id"
        " FROM messages WHERE conversation_id=? ORDER BY idx",
        (conv_id,),
    ).fetchall()
    atts = conn.execute(
        "SELECT message_id, source_ref, mime, size, hash, extracted_text"
        " FROM attachments WHERE conversation_id=? ORDER BY id",
        (conv_id,),
    ).fetchall()
    atts_by_msg: dict[int, list[dict]] = {}
    for a in atts:
        atts_by_msg.setdefault(a["message_id"], []).append({
            "source_ref": a["source_ref"], "mime": a["mime"],
            "size": a["size"], "hash": a["hash"],
            "extracted_text": a["extracted_text"],
        })
    return {
        "id": conv["id"],
        "source": conv["source"],
        "source_id": conv["source_id"],
        "title": conv["title"],
        "created_at": conv["created_at"],
        "updated_at": conv["updated_at"],
        "meta": json.loads(conv["meta"]),
        "messages": [
            {**dict(m), "attachments": atts_by_msg.get(m["id"], [])}
            for m in msgs
        ],
    }


def list_conversations(
    source: str | None = None,
    limit: int = 100,
    offset: int = 0,
    after: str | None = None,
) -> list[dict]:
    conn = connect()
    conds, params = [], []
    if source:
        conds.append("source = ?")
        params.append(source)
    if after:
        conds.append("updated_at >= ?")
        params.append(after)
    src_clause = ("WHERE " + " AND ".join(conds)) if conds else ""
    params += [limit, offset]
    rows = conn.execute(
        f"""SELECT c.id, c.source, c.title, c.created_at, c.updated_at, c.meta,
                   (SELECT COUNT(*) FROM messages m WHERE m.conversation_id = c.id) AS message_count
            FROM conversations c {src_clause}
            ORDER BY c.updated_at DESC LIMIT ? OFFSET ?""",
        params,
    ).fetchall()
    return [{**dict(r), "meta": json.loads(r["meta"])} for r in rows]


def list_items(
    source: str | None = None,
    kinds: list[str] | None = None,
    limit: int = 100,
    offset: int = 0,
    after: str | None = None,
    before: str | None = None,
) -> list[dict]:
    """Recent-activity listing: conversations + external items merged, newest
    first (the empty-query counterpart of search, so items-only sources like
    x/karakeep get a browsable list too).

    Two UNION ALL branches. Conversations come from the conversations table
    (message_count, and rows survive a missing items mirror — legacy DBs can
    lack it until rechunk self-heals); the items branch excludes
    kind='conversation' so mirror rows never duplicate them. Ordering and the
    after/before window both use COALESCE(updated_at, created_at) so the
    filter matches the date shown; rows with neither date (e.g. X likes)
    sort last (SQLite DESC puts NULL last) and are excluded once a date
    filter is set — same behaviour as _keyword_item_rows. Rows are shaped
    like search hits (conversation-only fields carry None on item rows)."""
    conn = connect()
    non_conv_kinds = [k for k in kinds if k != "conversation"] if kinds else None
    branches: list[str] = []
    params: list = []

    if kinds is None or "conversation" in kinds:
        filt = ""
        if source:
            filt += " AND c.source = ? "
            params.append(source)
        if after:
            filt += " AND COALESCE(c.updated_at, c.created_at) >= ? "
            params.append(after)
        if before:
            filt += " AND COALESCE(c.updated_at, c.created_at) <= ? "
            params.append(before)
        branches.append(f"""
            SELECT c.id AS conversation_id, i.id AS item_id,
                   'conversation' AS kind, c.source, c.title, NULL AS url,
                   c.source_id AS external_id, c.created_at, c.updated_at,
                   c.meta,
                   (SELECT COUNT(*) FROM messages m
                    WHERE m.conversation_id = c.id) AS message_count
            FROM conversations c
            LEFT JOIN items i ON i.source = c.source AND i.external_id = c.source_id
            WHERE 1=1 {filt}
        """)

    if kinds is None or non_conv_kinds:
        filt = " AND i.kind != 'conversation' "
        if non_conv_kinds:
            filt += f" AND i.kind IN ({','.join('?' * len(non_conv_kinds))}) "
            params.extend(non_conv_kinds)
        if source:
            filt += " AND i.source = ? "
            params.append(source)
        if after:
            filt += " AND COALESCE(i.updated_at, i.created_at) >= ? "
            params.append(after)
        if before:
            filt += " AND COALESCE(i.updated_at, i.created_at) <= ? "
            params.append(before)
        branches.append(f"""
            SELECT NULL AS conversation_id, i.id AS item_id,
                   i.kind, i.source, i.title, i.url,
                   i.external_id, i.created_at, i.updated_at,
                   i.meta,
                   NULL AS message_count
            FROM items i
            WHERE 1=1 {filt}
        """)

    if not branches:
        return []
    rows = conn.execute(
        f"""
        SELECT * FROM ({' UNION ALL '.join(branches)})
        ORDER BY COALESCE(updated_at, created_at) DESC, item_id DESC
        LIMIT ? OFFSET ?
        """,
        [*params, limit, offset],
    ).fetchall()
    return [
        {
            **dict(r),
            "url": _safe_external_url(r["url"]),
            "meta": json.loads(r["meta"] or "{}"),
        }
        for r in rows
    ]


def get_item(source: str, external_id: str) -> dict | None:
    """One item from the registry by its (source, external_id) key (M5).

    Read-only. Returns the items row with meta parsed, url passed through the
    same http(s) safety gate the search rows use, an assembled `body`
    (the redacted, deterministic index text for external kinds — reused from
    the chunker's source so callers get the same text that was indexed), and
    `conversation_id` when kind='conversation' (external_id == source_id) so
    the caller can fetch the full thread via get_conversation(). Returns None
    if no such item exists."""
    conn = connect()
    row = conn.execute(
        "SELECT id, kind, source, external_id, title, url, url_norm, doi,"
        " created_at, updated_at, meta FROM items WHERE source=? AND external_id=?",
        (source, external_id),
    ).fetchone()
    if row is None:
        return None
    meta = json.loads(row["meta"] or "{}")
    out = {
        "item_id": row["id"],
        "kind": row["kind"],
        "source": row["source"],
        "external_id": row["external_id"],
        "title": row["title"],
        "url": _safe_external_url(row["url"]),
        "doi": row["doi"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "meta": meta,
        "conversation_id": None,
        "body": None,
    }
    if row["kind"] == "conversation":
        conv = conn.execute(
            "SELECT id FROM conversations WHERE source=? AND source_id=?",
            (source, external_id),
        ).fetchone()
        out["conversation_id"] = conv["id"] if conv else None
    else:
        out["body"] = _item_index_text(row["title"], meta)
    return out


def get_item_by_id(item_id: int) -> dict | None:
    """One item by its items.id (HTTP detail endpoint). Thin wrapper that
    resolves the (source, external_id) key and delegates to get_item() so
    body assembly, URL gating and conversation_id resolution stay in one
    place. Returns None if no such item exists."""
    conn = connect()
    row = conn.execute(
        "SELECT source, external_id FROM items WHERE id = ?", (item_id,)
    ).fetchone()
    if row is None:
        return None
    return get_item(row["source"], row["external_id"])


def linked_items(item_id: int) -> list[dict]:
    """Items strongly linked to *item_id* via item_links (url/doi/github, M5).

    Read-only helper for build_context_pack: follows the persisted strong-match
    edges (D5 — same-source similarity is never stored, only these exact
    matches). Returns the neighbour items (meta parsed, url gated) each tagged
    with `link_via`. Undirected: matches rows where item_id is on either side."""
    conn = connect()
    rows = conn.execute(
        """SELECT i.id, i.kind, i.source, i.external_id, i.title, i.url,
                  i.doi, i.created_at, i.updated_at, i.meta, l.link_via
           FROM item_links l
           JOIN items i ON i.id = CASE WHEN l.a_id=? THEN l.b_id ELSE l.a_id END
           WHERE l.a_id=? OR l.b_id=?""",
        (item_id, item_id, item_id),
    ).fetchall()
    return [
        {
            "item_id": r["id"],
            "kind": r["kind"],
            "source": r["source"],
            "external_id": r["external_id"],
            "title": r["title"],
            "url": _safe_external_url(r["url"]),
            "doi": r["doi"],
            "created_at": r["created_at"],
            "updated_at": r["updated_at"],
            "meta": json.loads(r["meta"] or "{}"),
            "link_via": r["link_via"],
        }
        for r in rows
    ]


def stats() -> dict:
    conn = connect()
    rows = conn.execute(
        """SELECT c.source, COUNT(DISTINCT c.id) AS conversations, COUNT(m.id) AS messages
           FROM conversations c LEFT JOIN messages m ON m.conversation_id = c.id
           GROUP BY c.source"""
    ).fetchall()
    # items breakdown (M1): registry counts per kind/source + link total, so
    # /api/stats shows the cross-source state (DESIGN.md §7 M1 完了条件).
    item_rows = conn.execute(
        "SELECT kind, source, COUNT(*) AS count FROM items GROUP BY kind, source"
        " ORDER BY kind, source"
    ).fetchall()
    link_count = conn.execute("SELECT COUNT(*) FROM item_links").fetchone()[0]
    return {
        "sources": [dict(r) for r in rows],
        "items": [dict(r) for r in item_rows],
        "item_links": link_count,
    }


def iter_export_conversations(
    source: str | None = None,
    after: str | None = None,
    before: str | None = None,
    conversation_id: int | None = None,
):
    """Yield conversations (same shape as get_conversation()) matching the
    filters, ordered by updated_at. The shared filter/fetch layer behind
    export_jsonl — P1-G's Markdown export will reuse it. Filters compose
    with AND. `after` / `before` compare against updated_at as ISO8601
    strings (lexical comparison works for normalized ISO timestamps).
    """
    conn = connect()
    conds, params = [], []
    if source:
        conds.append("source = ?")
        params.append(source)
    if after:
        conds.append("updated_at >= ?")
        params.append(after)
    if before:
        conds.append("updated_at <= ?")
        params.append(before)
    if conversation_id is not None:
        conds.append("id = ?")
        params.append(conversation_id)
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    ids = conn.execute(
        f"SELECT id FROM conversations {where} ORDER BY updated_at",
        params,
    ).fetchall()
    for row in ids:
        conv = get_conversation(row["id"])
        if conv:
            yield conv


def iter_export_jsonl(
    source: str | None = None,
    after: str | None = None,
    before: str | None = None,
    conversation_id: int | None = None,
):
    """Yield one JSONL line (incl. trailing newline) per matching conversation.

    Shared by export_jsonl (CLI, writes to a file/stdout) and the streaming
    /api/export endpoint (backlog A5) — one record-shaping implementation so
    the two paths cannot drift. Streams one conversation at a time so large
    archives do not need to fit in memory.

    Output shape: `{schema, kind, source, source_id, title, created_at,
    updated_at, meta, messages, derived}`. `derived` is reserved for future
    Cairn-computed fields (embeddings, segments, ...) so readers can tell
    original-from-source data apart from extensions.
    """
    for conv in iter_export_conversations(source, after, before, conversation_id):
        record = {
            "schema": "cairn.export.v1",
            "kind": "conversation",
            "source": conv["source"],
            "source_id": conv["source_id"],
            "title": conv["title"],
            "created_at": conv["created_at"],
            "updated_at": conv["updated_at"],
            "meta": conv["meta"],
            "messages": [
                {
                    "idx": m["idx"],
                    "role": m["role"],
                    "text": m["text"],
                    "created_at": m["created_at"],
                    "source_message_id": m["source_message_id"],
                }
                for m in conv["messages"]
            ],
            "derived": {},
        }
        yield json.dumps(record, ensure_ascii=False) + "\n"


def export_jsonl(
    out,
    source: str | None = None,
    after: str | None = None,
    before: str | None = None,
    conversation_id: int | None = None,
) -> int:
    """Write filtered conversations to `out` as JSONL. Returns the count."""
    n = 0
    for line in iter_export_jsonl(source, after, before, conversation_id):
        out.write(line)
        n += 1
    return n


def iter_export_markdown(
    source: str | None = None,
    after: str | None = None,
    before: str | None = None,
    conversation_id: int | None = None,
):
    """Yield one Markdown chunk per matching conversation (backlog A5, shared
    with export_markdown). Shares the filter layer with iter_export_jsonl
    (iter_export_conversations), so flags behave identically.

    Per-conversation layout: `# title` + a bullet list of source / source_id /
    created_at / updated_at, then each message as `## role — timestamp` with
    the body preserved verbatim. Multiple conversations are separated by a
    horizontal rule so the stream can be split (e.g. for Obsidian).
    """
    for i, conv in enumerate(iter_export_conversations(source, after, before, conversation_id)):
        chunk = "\n---\n\n" if i > 0 else ""
        chunk += f"# {conv['title']}\n\n"
        chunk += f"- source: {conv['source']}\n"
        chunk += f"- source_id: {conv['source_id']}\n"
        if conv["created_at"]:
            chunk += f"- created_at: {conv['created_at']}\n"
        if conv["updated_at"]:
            chunk += f"- updated_at: {conv['updated_at']}\n"
        chunk += "\n"
        for m in conv["messages"]:
            header = f"## {m['role']}"
            if m["created_at"]:
                header += f" — {m['created_at']}"
            chunk += f"{header}\n\n"
            chunk += m["text"].rstrip()
            chunk += "\n\n"
        yield chunk


def export_markdown(
    out,
    source: str | None = None,
    after: str | None = None,
    before: str | None = None,
    conversation_id: int | None = None,
) -> int:
    """Write filtered conversations to `out` as human-readable Markdown.
    Returns the number of conversations written."""
    n = 0
    for chunk in iter_export_markdown(source, after, before, conversation_id):
        out.write(chunk)
        n += 1
    return n


def backup(out_path: str | None = None, *, with_blobs: bool = False) -> str:
    """Create a consistent single-file copy of the DB and return its path.

    Checkpoints the WAL first so the copied main file is self-contained.
    Default destination is `<db>.backup-<timestamp>` (microsecond precision —
    two backups in the same second must not overwrite each other, and the
    `.attachments` sibling below would collide on copytree).
    The copy contains plaintext conversation data, so it is locked down to
    0600. Restore by copying the file back or pointing CAIRN_DB at it.

    with_blobs (backlog A1): also copy the attachments blob store to the
    sibling directory `<out>.attachments/` so DB + blobs travel as a pair —
    exactly the colocate case attachments.root_dir() anticipates. Blobs are
    content-addressed (sha256 filenames), so a plain copytree is consistent
    even while ingest is running. A missing store is not an error: the DB
    copy alone is still a valid backup.
    """
    conn = connect()
    db_path = os.path.abspath(DB_PATH)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.Error:
        pass  # best-effort; copy proceeds regardless
    if out_path is None:
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        out_path = f"{db_path}.backup-{stamp}"
    out_path = os.path.abspath(out_path)
    shutil.copy2(db_path, out_path)
    try:
        os.chmod(out_path, 0o600)
    except OSError:
        pass
    if with_blobs:
        from . import attachments as _store
        src = _store.root_dir()
        if os.path.isdir(src):
            dest = out_path + ".attachments"
            shutil.copytree(src, dest, dirs_exist_ok=False)
            try:
                os.chmod(dest, 0o700)
            except OSError:
                pass
    return out_path


def prune_backups(keep: int) -> list[str]:
    """Delete all but the newest *keep* auto-named backups (backlog A8).

    Only default-pattern siblings `<db>.backup-<stamp>` are candidates —
    a backup written to an explicit --out path is user-managed and is never
    touched. The fixed-width timestamp makes lexicographic order
    chronological, so a plain sort suffices. Each pruned backup's
    `.attachments` sibling (A1) is removed with it: DB + blobs leave as the
    pair they arrived as, never one without the other. Returns the deleted
    backup paths, oldest first."""
    if keep < 1:
        raise ValueError("keep must be >= 1")
    db_path = os.path.abspath(DB_PATH)
    candidates = sorted(
        p for p in glob.glob(db_path + ".backup-*")
        if os.path.isfile(p) and not p.endswith(".attachments")
    )
    deleted: list[str] = []
    for p in candidates[:-keep]:
        os.remove(p)
        blobs = p + ".attachments"
        if os.path.isdir(blobs):
            shutil.rmtree(blobs)
        deleted.append(p)
    return deleted


def integrity_check() -> dict:
    """Read-only consistency audit (P1-D). Returns {ok, checks, problems}.

    Covers: SQLite structural integrity, orphan messages/attachments, FTS row
    count + structural integrity, duplicate stable conversation ids, and blank
    source identifiers. Makes no changes to the DB."""
    conn = connect()
    problems: list[str] = []
    checks: dict = {}

    # 1. SQLite structural integrity.
    ic = [r[0] for r in conn.execute("PRAGMA integrity_check").fetchall()]
    checks["sqlite_integrity_check"] = ic
    if ic != ["ok"]:
        problems.append(f"PRAGMA integrity_check: {ic}")

    # 2. Orphan messages (parent conversation missing).
    orphan_msgs = conn.execute(
        "SELECT COUNT(*) FROM messages m "
        "LEFT JOIN conversations c ON c.id = m.conversation_id WHERE c.id IS NULL"
    ).fetchone()[0]
    checks["orphan_messages"] = orphan_msgs
    if orphan_msgs:
        problems.append(f"{orphan_msgs} orphan messages (no parent conversation)")

    # 3. FTS index consistency. Count indexed documents via the _docsize shadow
    #    table (a plain `COUNT(*) FROM messages_fts` proxies to the content
    #    table and always matches, so it can't reveal a desync). Then run
    #    FTS5's own structural integrity-check.
    msg_count = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    fts_count = conn.execute("SELECT COUNT(*) FROM messages_fts_docsize").fetchone()[0]
    checks["messages"] = msg_count
    checks["messages_fts"] = fts_count
    if msg_count != fts_count:
        problems.append(
            f"FTS row count mismatch: messages={msg_count} indexed={fts_count}"
        )
    try:
        # This special command makes no changes; roll back the implicit
        # transaction sqlite3 opens for an INSERT-shaped statement.
        conn.execute("INSERT INTO messages_fts(messages_fts) VALUES('integrity-check')")
        conn.rollback()
        checks["fts_integrity"] = "ok"
    except sqlite3.DatabaseError as e:
        conn.rollback()
        checks["fts_integrity"] = str(e)
        problems.append(f"FTS integrity-check failed: {e}")

    # 4. Duplicate stable conversation ids (UNIQUE should prevent these).
    dups = conn.execute(
        "SELECT source, source_id, COUNT(*) AS n FROM conversations "
        "GROUP BY source, source_id HAVING n > 1"
    ).fetchall()
    checks["duplicate_conversation_ids"] = len(dups)
    for d in dups:
        problems.append(
            f"duplicate stable id (source={d['source']}, source_id={d['source_id']}) x{d['n']}"
        )

    # 5. Blank source identifiers (referential sanity).
    blank = conn.execute(
        "SELECT COUNT(*) FROM conversations "
        "WHERE source IS NULL OR source='' OR source_id IS NULL OR source_id=''"
    ).fetchone()[0]
    checks["blank_source_or_source_id"] = blank
    if blank:
        problems.append(f"{blank} conversations with blank source/source_id")

    # 6. Orphan attachments — only if the table exists (P1-H not yet implemented).
    has_attachments = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='attachments'"
    ).fetchone()[0]
    if has_attachments:
        orphan_att = conn.execute(
            "SELECT COUNT(*) FROM attachments a "
            "LEFT JOIN conversations c ON c.id = a.conversation_id WHERE c.id IS NULL"
        ).fetchone()[0]
        checks["orphan_attachments"] = orphan_att
        if orphan_att:
            problems.append(f"{orphan_att} orphan attachments")

    # 7. Orphan chunks — only if the table exists (P2-1a not yet migrated).
    has_chunks = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='chunks'"
    ).fetchone()[0]
    if has_chunks:
        orphan_chunks = conn.execute(
            "SELECT COUNT(*) FROM chunks ch "
            "LEFT JOIN messages m ON m.id = ch.message_id WHERE m.id IS NULL"
        ).fetchone()[0]
        checks["orphan_chunks"] = orphan_chunks
        if orphan_chunks:
            problems.append(f"{orphan_chunks} orphan chunks")

    # 8. Orphan embeddings — only if the table exists (P2-1b not yet migrated).
    has_embeddings = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='embeddings'"
    ).fetchone()[0]
    if has_embeddings:
        orphan_embeddings = conn.execute(
            "SELECT COUNT(*) FROM embeddings e "
            "LEFT JOIN chunks ch ON ch.id = e.chunk_id WHERE ch.id IS NULL"
        ).fetchone()[0]
        checks["orphan_embeddings"] = orphan_embeddings
        if orphan_embeddings:
            problems.append(f"{orphan_embeddings} orphan embeddings")

    # M0: items registry consistency (only if the table exists — pre-v11 DBs).
    has_items = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='items'"
    ).fetchone()[0]
    if has_items:
        # (i) Every conversation must have an items row. Missing rows would
        # stall cross-source RRF and break M1's URL/DOI joins. Hard problem.
        conv_without_item = conn.execute(
            "SELECT COUNT(*) FROM conversations c "
            "LEFT JOIN items i "
            "  ON i.source = c.source AND i.external_id = c.source_id "
            "WHERE i.id IS NULL"
        ).fetchone()[0]
        checks["conversations_missing_item"] = conv_without_item
        if conv_without_item:
            problems.append(
                f"{conv_without_item} conversations without a matching items row"
            )

        # (ii) Every chunk must resolve to an items row (item_id NOT NULL).
        # NULL means either the v11 backfill missed it or an ingest bypassed
        # _ensure_item_for_conversation; either way, cross-source join breaks.
        if has_chunks:
            chunks_missing_item_id = conn.execute(
                "SELECT COUNT(*) FROM chunks WHERE item_id IS NULL"
            ).fetchone()[0]
            checks["chunks_missing_item_id"] = chunks_missing_item_id
            if chunks_missing_item_id:
                problems.append(
                    f"{chunks_missing_item_id} chunks with NULL item_id"
                )

        # (iii) items(kind='conversation') rows whose conversation is gone —
        # info only. No delete path for conversations exists today; this is a
        # forward-looking detector for whenever one lands.
        item_without_conv = conn.execute(
            "SELECT COUNT(*) FROM items i "
            "LEFT JOIN conversations c "
            "  ON c.source = i.source AND c.source_id = i.external_id "
            "WHERE i.kind = 'conversation' AND c.id IS NULL"
        ).fetchone()[0]
        checks["items_without_conversation"] = item_without_conv

        # (iv) items vs conversations title/content_hash drift — info only.
        # admin.redact-apply mutates conversations directly (see helper
        # docstring). admin.py is frozen through M5, DESIGN.md §5.7, so this
        # is observed, not fixed at ingest time.
        drift = conn.execute(
            "SELECT COUNT(*) FROM items i "
            "JOIN conversations c "
            "  ON c.source = i.source AND c.source_id = i.external_id "
            "WHERE i.kind = 'conversation' "
            "  AND (i.title IS NOT c.title OR i.content_hash IS NOT c.content_hash)"
        ).fetchone()[0]
        checks["items_conversation_drift"] = drift

    # 8b. Attachment blob store (P1-J): compare hashes in the attachments
    # table against bytes on disk. Two failure modes worth surfacing:
    # (a) a row references a hash whose file is missing — bytes lost or
    #     never stored
    # (b) the store has a blob no row references — orphan, safe to GC
    # Neither is appended to `problems`: missing blobs may be intentional
    # (metadata-only sources like Claude UUID refs), and orphans waste disk
    # but don't corrupt search.
    if has_attachments:
        from . import attachments as _store
        attached_hashes = {
            r[0] for r in conn.execute(
                "SELECT DISTINCT hash FROM attachments WHERE hash IS NOT NULL"
            ).fetchall()
        }
        on_disk = set(_store.iter_hashes())
        checks["attachment_blobs_on_disk"] = len(on_disk)
        checks["attachment_blobs_missing"] = sum(
            1 for h in attached_hashes if h not in on_disk
        )
        checks["attachment_blobs_orphan"] = sum(
            1 for h in on_disk if h not in attached_hashes
        )

    # 9. Orphan vector-index rows — vec0 doesn't honour FK cascade, so chunks
    # deleted via cascade can leave stale rows behind. Reported as info, not
    # a hard problem; `admin rebuild-vector-index` clears them.
    has_chunk_vecs = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='chunk_vecs'"
    ).fetchone()[0]
    if has_chunk_vecs:
        orphan_vecs = conn.execute(
            "SELECT COUNT(*) FROM chunk_vecs v "
            "LEFT JOIN chunks ch ON ch.id = v.rowid WHERE ch.id IS NULL"
        ).fetchone()[0]
        checks["orphan_vector_index"] = orphan_vecs
        # not appended to problems: orphans are tolerated until rebuild

    return {"ok": not problems, "checks": checks, "problems": problems}


def file_state(path: str) -> tuple | None:
    row = connect().execute(
        "SELECT mtime, size FROM ingest_files WHERE path=?", (path,)
    ).fetchone()
    return (row["mtime"], row["size"]) if row else None


def record_file_state(path: str, mtime: float, size: int) -> None:
    conn = connect()
    with conn:
        conn.execute(
            "INSERT INTO ingest_files (path, mtime, size) VALUES (?,?,?) "
            "ON CONFLICT(path) DO UPDATE SET mtime=excluded.mtime, size=excluded.size",
            (path, mtime, size),
        )
