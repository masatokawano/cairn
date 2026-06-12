"""Sync CLI session logs (claude / codex) into the DB.

Strategy: poll-based. scan_once() walks the log directories, compares each
file's (mtime, size) against ingest_files, and re-parses only changed files.
A background thread calls scan_once() every CAIRN_SYNC_INTERVAL seconds
(default 60). Polling instead of FS events keeps deps minimal and survives
editor/atomic-rename weirdness.
"""
from __future__ import annotations

import logging
import os
import threading

from . import db
from .parsers import claude_cli, codex_cli

log = logging.getLogger("cairn.sync")

CLAUDE_PROJECTS_DIR = os.environ.get(
    "CAIRN_CLAUDE_DIR", os.path.expanduser("~/.claude/projects")
)
CODEX_SESSIONS_DIR = os.environ.get(
    "CAIRN_CODEX_DIR", os.path.expanduser("~/.codex/sessions")
)
SYNC_INTERVAL = float(os.environ.get("CAIRN_SYNC_INTERVAL", "60"))


def _iter_jsonl(root: str):
    if not os.path.isdir(root):
        return
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            if name.endswith(".jsonl"):
                yield os.path.join(dirpath, name)


# Serializes ingest work: the background sync loop, POST /api/sync and
# POST /api/import all write to the same SQLite DB.
ingest_lock = threading.Lock()


def scan_once() -> dict:
    """Scan both CLI log trees; import changed files. Returns stats.
    Blocks until the ingest lock is free."""
    with ingest_lock:
        return _scan()


def try_scan_once() -> dict | None:
    """Like scan_once(), but returns None instead of waiting if another
    sync/import is already running (used by POST /api/sync → 409)."""
    if not ingest_lock.acquire(blocking=False):
        return None
    try:
        return _scan()
    finally:
        ingest_lock.release()


def _scan() -> dict:
    totals = {"files_scanned": 0, "files_imported": 0,
              "inserted": 0, "updated": 0, "skipped": 0, "warnings": []}
    for root, parser in (
        (CLAUDE_PROJECTS_DIR, claude_cli),
        (CODEX_SESSIONS_DIR, codex_cli),
    ):
        for path in _iter_jsonl(root):
            totals["files_scanned"] += 1
            try:
                st = os.stat(path)
            except OSError:
                continue
            if db.file_state(path) == (st.st_mtime, st.st_size):
                continue
            try:
                with open(path, encoding="utf-8", errors="replace") as f:
                    content = f.read()
                result = parser.parse_file(path, content)
            except Exception as e:  # noqa: BLE001 — one bad file must not stop the sync
                totals["warnings"].append(f"{path}: {e}")
                continue
            stats = db.upsert_conversations(result.conversations)
            db.record_file_state(path, st.st_mtime, st.st_size)
            totals["files_imported"] += 1
            for k in ("inserted", "updated", "skipped"):
                totals[k] += stats[k]
            totals["warnings"].extend(result.warnings[:5])
    return totals


def start_background_sync() -> threading.Thread:
    def loop():
        stop = threading.Event()
        while True:
            try:
                stats = scan_once()
                if stats["files_imported"]:
                    log.info("sync: %s", stats)
            except Exception:  # noqa: BLE001
                log.exception("sync failed")
            stop.wait(SYNC_INTERVAL)

    t = threading.Thread(target=loop, daemon=True, name="cairn-sync")
    t.start()
    return t
