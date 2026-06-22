"""SQLite layer: schema, diff import, and search (FTS5 trigram + LIKE fallback)."""
from __future__ import annotations

import datetime
import json
import logging
import os
import re
import shutil
import sqlite3
import threading

from . import redact

log = logging.getLogger("cairn.db")

DB_PATH = os.environ.get(
    "CAIRN_DB", os.path.join(os.path.dirname(__file__), "..", "data", "cairn.db")
)

_local = threading.local()

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
    created_at TEXT
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
_SCHEMA_VERSION = 1
_MIGRATIONS: list[tuple[int, str]] = []


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
        # A fresh DB (no tables yet) is built from the latest _SCHEMA and
        # stamped directly — it must NOT run migrations meant for older shapes.
        # An existing DB (tables present, possibly pre-versioning at v0) is
        # migrated up to _SCHEMA_VERSION.
        is_fresh = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='conversations'"
        ).fetchone()[0] == 0
        conn.executescript(_SCHEMA)
        if is_fresh:
            conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
        else:
            _apply_migrations(conn, db_path)
        _restrict_permissions(db_path)
        _local.conn = conn
    return conn


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
            if row:
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
            conn.executemany(
                "INSERT INTO messages (conversation_id, idx, role, text, created_at) VALUES (?,?,?,?,?)",
                [(conv_id, i, m.role, m.text, m.created_at) for i, m in enumerate(pc.messages)],
            )
    return stats


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
    source: str | None = None,
    limit: int = 50,
    offset: int = 0,
    after: str | None = None,
    before: str | None = None,
) -> list[dict]:
    """Search messages; group hits by conversation (best hit per conversation).

    Multi-term queries (whitespace-separated) are AND. Queries where every
    term has >=3 chars use FTS5 trigram; otherwise LIKE scan (fine at
    personal-archive scale). after/before filter on the conversation's
    updated_at (ISO8601 string comparison).
    """
    conn = connect()
    terms = [t for t in q.split() if t]
    if not terms:
        return []
    use_fts = all(len(t) >= 3 for t in terms)

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
                       m.id AS message_id, m.role, m.created_at AS msg_created_at,
                       m.text AS snip,
                       COUNT(*) OVER (PARTITION BY c.id) AS hit_count,
                       ROW_NUMBER() OVER (PARTITION BY c.id ORDER BY m.idx) AS rn
                FROM messages m
                JOIN conversations c ON c.id = m.conversation_id
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
            "source": r["source"],
            "title": r["title"],
            "updated_at": r["updated_at"] or r["created_at"],
            "meta": json.loads(r["meta"]),
            "snippet": snip,
            "role": r["role"],
            "message_id": r["message_id"],
            "hit_count": r["hit_count"],
        })
    return results


def get_conversation(conv_id: int) -> dict | None:
    conn = connect()
    conv = conn.execute("SELECT * FROM conversations WHERE id=?", (conv_id,)).fetchone()
    if not conv:
        return None
    msgs = conn.execute(
        "SELECT id, idx, role, text, created_at FROM messages WHERE conversation_id=? ORDER BY idx",
        (conv_id,),
    ).fetchall()
    return {
        "id": conv["id"],
        "source": conv["source"],
        "source_id": conv["source_id"],
        "title": conv["title"],
        "created_at": conv["created_at"],
        "updated_at": conv["updated_at"],
        "meta": json.loads(conv["meta"]),
        "messages": [dict(m) for m in msgs],
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


def stats() -> dict:
    conn = connect()
    rows = conn.execute(
        """SELECT c.source, COUNT(DISTINCT c.id) AS conversations, COUNT(m.id) AS messages
           FROM conversations c LEFT JOIN messages m ON m.conversation_id = c.id
           GROUP BY c.source"""
    ).fetchall()
    return {"sources": [dict(r) for r in rows]}


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
