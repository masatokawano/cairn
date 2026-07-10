"""Sync CLI session logs (claude / codex) into the DB.

Strategy: poll-based. scan_once() walks the log directories, compares each
file's (mtime, size) against ingest_files, and re-parses only changed files.
A background thread calls scan_once() every CAIRN_SYNC_INTERVAL seconds
(default 60). Polling instead of FS events keeps deps minimal and survives
editor/atomic-rename weirdness.

The same scan also runs from the hourly `cairn sync all` LaunchAgent — a
separate process, which `ingest_lock` (threading.Lock) cannot see. Both
entry points therefore additionally take an OS-level flock on a sidecar
file next to the DB (D12), so the server's 60s poll and the LaunchAgent
never process the same log concurrently (duplicate import_runs rows /
SQLite write contention).
"""
from __future__ import annotations

import fcntl
import hashlib
import logging
import os
import threading
from contextlib import contextmanager

from . import db
from .parsers import PARSER_VERSION, claude_cli, codex_cli

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


def _lock_path() -> str:
    """Sidecar lock file next to the DB (derived per call — CAIRN_DB varies
    per test). The DB file itself can't be flocked: SQLite owns its locks."""
    return os.path.join(
        os.path.dirname(os.path.abspath(db.DB_PATH)), ".cairn-sync.lock"
    )


@contextmanager
def _process_lock(*, blocking: bool):
    """Cross-process exclusive lock around a scan (D12). Yields True when
    held; with blocking=False yields False instead of waiting when another
    process (e.g. the hourly LaunchAgent) is already scanning."""
    path = _lock_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def scan_once() -> dict:
    """Scan both CLI log trees; import changed files. Returns stats.
    Blocks until both the in-process ingest lock and the cross-process
    flock are free."""
    with ingest_lock:
        with _process_lock(blocking=True):
            return _scan()


def try_scan_once() -> dict | None:
    """Like scan_once(), but returns None instead of waiting if another
    sync/import is already running — in this process (POST /api/sync → 409)
    or in another one (the hourly `cairn sync all` LaunchAgent)."""
    if not ingest_lock.acquire(blocking=False):
        return None
    try:
        with _process_lock(blocking=False) as held:
            if not held:
                return None
            return _scan()
    finally:
        ingest_lock.release()


def _scan() -> dict:
    totals = {"files_scanned": 0, "files_imported": 0,
              "inserted": 0, "updated": 0, "skipped": 0, "warnings": []}
    for root, parser, src in (
        (CLAUDE_PROJECTS_DIR, claude_cli, "claude_cli"),
        (CODEX_SESSIONS_DIR, codex_cli, "codex_cli"),
    ):
        for path in _iter_jsonl(root):
            totals["files_scanned"] += 1
            try:
                st = os.stat(path)
            except OSError:
                continue
            if db.file_state(path) == (st.st_mtime, st.st_size):
                continue
            started = db.utcnow_iso()
            try:
                with open(path, encoding="utf-8", errors="replace") as f:
                    content = f.read()
                result = parser.parse_file(path, content)
            except Exception as e:  # noqa: BLE001 — one bad file must not stop the sync
                totals["warnings"].append(f"{path}: {e}")
                db.record_import_run(
                    source=src, input_name=path, started_at=started,
                    completed_at=db.utcnow_iso(), status="error", error=str(e),
                )
                continue
            stats = db.upsert_conversations(result.conversations)
            db.record_file_state(path, st.st_mtime, st.st_size)
            db.record_import_run(
                source=src, input_name=path, started_at=started,
                completed_at=db.utcnow_iso(), parser_version=PARSER_VERSION,
                inserted=stats["inserted"], updated=stats["updated"], skipped=stats["skipped"],
                conversations=len(result.conversations), warnings=result.warnings,
                content_hash=hashlib.sha256(content.encode("utf-8", "replace")).hexdigest(),
                status="ok",
            )
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
