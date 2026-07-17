"""Cairn API server. Serves /api/* and the built frontend (frontend/dist).

Security posture (see SECURITY.md): the API has no auth, so it must stay
local. A middleware rejects requests whose Host (and, for mutations,
Origin) is not localhost — this also blocks DNS-rebinding and cross-site
blind POSTs from browsers. Extra hostnames (e.g. host.docker.internal for
testing) can be allowed via CAIRN_ALLOW_HOSTS, with a loud warning.
"""
from __future__ import annotations

import hashlib
import logging
import os
from contextlib import asynccontextmanager
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import cli_sync, db
from .parsers import PARSER_VERSION, FileTooLargeError, UnknownFormatError, parse_upload

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("cairn")

MAX_UPLOAD_BYTES = int(os.environ.get("CAIRN_MAX_UPLOAD_MB", "500")) * 1024 * 1024

_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
_EXTRA_HOSTS = {
    h.strip().lower() for h in os.environ.get("CAIRN_ALLOW_HOSTS", "").split(",") if h.strip()
}
_ALLOWED_HOSTS = _LOCAL_HOSTS | _EXTRA_HOSTS


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """FastAPI lifespan replacing the deprecated on_event hooks.

    Startup: opens the SQLite connection for the main thread and kicks off
    the CLI background-sync daemon. Shutdown is a no-op — the sync thread
    is a daemon (dies with the process), and SQLite cleans up naturally.
    """
    db.connect()
    cli_sync.start_background_sync()
    if _EXTRA_HOSTS:
        log.warning(
            "CAIRN_ALLOW_HOSTS=%s — localhost 以外の Host を許可しています。"
            "会話アーカイブが他ホストから読める可能性があります。検証用途以外では解除してください。",
            ",".join(sorted(_EXTRA_HOSTS)),
        )
    log.info("Cairn API ready (bind to 127.0.0.1; do not expose to LAN)")
    yield


app = FastAPI(title="Cairn", lifespan=lifespan)


def _hostname(value: str) -> str | None:
    """Extract the hostname from a Host header or an Origin URL."""
    if not value:
        return None
    if "://" not in value:
        value = "//" + value
    try:
        return urlparse(value).hostname
    except ValueError:
        return None


@app.middleware("http")
async def local_only(request: Request, call_next):
    host = _hostname(request.headers.get("host", ""))
    if host is None or host.lower() not in _ALLOWED_HOSTS:
        return JSONResponse(
            status_code=403,
            content={"detail": "Cairn はローカル専用です (Host ヘッダが localhost ではありません)"},
        )
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        # Browsers always attach Origin to cross-origin POSTs; curl/CLI omit
        # it. Reject a present-but-foreign Origin (CSRF / blind POST guard).
        origin = request.headers.get("origin")
        if origin and origin != "null":
            origin_host = _hostname(origin)
            if origin_host is None or origin_host.lower() not in _ALLOWED_HOSTS:
                return JSONResponse(
                    status_code=403,
                    content={"detail": "Origin が localhost ではないため拒否しました"},
                )
    return await call_next(request)


async def _read_upload_capped(file: UploadFile) -> bytes:
    chunks, total = [], 0
    while True:
        chunk = await file.read(1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_UPLOAD_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"ファイルが大きすぎます (上限 {MAX_UPLOAD_BYTES // (1024 * 1024)}MB)。"
                       "上限は環境変数 CAIRN_MAX_UPLOAD_MB で変更できます",
            )
        chunks.append(chunk)
    return b"".join(chunks)


@app.post("/api/import")
async def import_file(file: UploadFile, request: Request):
    content_length = request.headers.get("content-length")
    if content_length and content_length.isdigit() and int(content_length) > MAX_UPLOAD_BYTES + 4096:
        raise HTTPException(
            status_code=413,
            detail=f"ファイルが大きすぎます (上限 {MAX_UPLOAD_BYTES // (1024 * 1024)}MB)",
        )
    raw = await _read_upload_capped(file)
    name = file.filename or "upload.json"
    started = db.utcnow_iso()
    content_hash = hashlib.sha256(raw).hexdigest()

    def _record_error(status_code: int, message: str):
        db.record_import_run(
            source="upload", input_name=name, started_at=started,
            completed_at=db.utcnow_iso(), content_hash=content_hash,
            status="error", error=message,
        )
        return HTTPException(status_code=status_code, detail=message)

    try:
        result = parse_upload(name, raw)
    except FileTooLargeError as e:
        raise _record_error(413, str(e))
    except UnknownFormatError as e:
        raise _record_error(422, str(e))
    except Exception as e:  # noqa: BLE001
        raise _record_error(422, f"パースに失敗しました: {e}")
    with cli_sync.ingest_lock:
        stats = db.upsert_conversations(result.conversations)
    db.record_import_run(
        source="upload", input_name=name, started_at=started,
        completed_at=db.utcnow_iso(), parser_version=PARSER_VERSION,
        inserted=stats["inserted"], updated=stats["updated"], skipped=stats["skipped"],
        conversations=len(result.conversations), warnings=result.warnings,
        content_hash=content_hash, status="ok",
    )
    return {
        "filename": file.filename,
        "conversations": len(result.conversations),
        **stats,
        "warnings": result.warnings[:20],
    }


@app.post("/api/sync")
def sync_now():
    stats = cli_sync.try_scan_once()
    if stats is None:
        raise HTTPException(status_code=409, detail="同期は既に実行中です")
    return stats


# items.kind values for /api/search filtering. Named distinctly from the
# Phase-3 _VALID_KINDS below (assertion kinds) — module-level rebinding
# would otherwise silently swap the sets.
_SEARCH_KINDS = {"conversation", "bookmark", "reference", "note", "social_post"}


def _parse_kinds(kinds: str | None) -> list[str] | None:
    """Comma-separated kinds param → validated list (shared by /api/search
    and /api/items so both endpoints accept exactly the same vocabulary)."""
    if not kinds:
        return None
    kind_list = [k.strip() for k in kinds.split(",") if k.strip()]
    bad = sorted(set(kind_list) - _SEARCH_KINDS)
    if bad:
        # explicit 422 rather than a silent empty result, mirroring the
        # mode pattern check — typos should fail loud
        raise HTTPException(
            status_code=422,
            detail=f"unknown kinds {bad}; valid: {sorted(_SEARCH_KINDS)}",
        )
    return kind_list or None


@app.get("/api/search")
def search(
    q: str = Query(..., min_length=1),
    source: str | None = None,
    kinds: str | None = Query(
        None,
        description="comma-separated items.kind filter, e.g. 'conversation,bookmark'",
    ),
    limit: int = Query(50, le=200),
    offset: int = 0,
    mode: str = Query("keyword", pattern="^(keyword|semantic|hybrid)$"),
    after: str | None = None,
    before: str | None = None,
):
    kind_list = _parse_kinds(kinds)
    try:
        results = db.search(
            q, mode=mode, source=source, kinds=kind_list, limit=limit,
            offset=offset, after=after, before=before,
        )
    except RuntimeError as exc:
        # The only RuntimeError db.search raises today is the "no embeddings
        # yet" path from _active_embedding_provider(). Turning it into a 400
        # with the message intact lets the UI's generic error renderer show
        # the actionable hint ("run admin reindex first") instead of HTTP 500.
        raise HTTPException(status_code=400, detail=str(exc))
    return {"query": q, "mode": mode, "results": results}


@app.get("/api/conversations")
def conversations(source: str | None = None, limit: int = Query(100, le=500), offset: int = 0):
    return {"results": db.list_conversations(source=source, limit=limit, offset=offset)}


@app.get("/api/conversations/{conv_id}")
def conversation(conv_id: int):
    conv = db.get_conversation(conv_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    return conv


@app.get("/api/items")
def items(
    source: str | None = None,
    kinds: str | None = Query(
        None,
        description="comma-separated items.kind filter, e.g. 'conversation,bookmark'",
    ),
    limit: int = Query(100, le=500),
    offset: int = 0,
    after: str | None = None,
    before: str | None = None,
):
    return {
        "results": db.list_items(
            source=source, kinds=_parse_kinds(kinds), limit=limit,
            offset=offset, after=after, before=before,
        )
    }


@app.get("/api/items/{item_id}")
def item_detail(item_id: int):
    it = db.get_item_by_id(item_id)
    if it is None:
        raise HTTPException(status_code=404, detail="item not found")
    return it


@app.get("/api/stats")
def stats():
    return db.stats()


@app.get("/api/import-runs")
def import_runs(
    limit: int = Query(50, le=500), offset: int = 0, source: str | None = None
):
    return {"results": db.list_import_runs(limit=limit, offset=offset, source=source)}


# ---------------------------------------------------------------------------
# Extractions API (P3-E)
# ---------------------------------------------------------------------------

@app.get("/api/conversations/{conv_id}/extractions")
def get_extractions(conv_id: int):
    """Return segments and assertions for a conversation."""
    conv = db.get_conversation(conv_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    import json
    segments = db.list_segments(conversation_id=conv_id)
    for seg in segments:
        seg["assertions"] = db.list_assertions(segment_id=seg["id"])
        for a in seg["assertions"]:
            try:
                a["supporting_message_ids"] = json.loads(a["supporting_message_ids"] or "[]")
            except Exception:
                a["supporting_message_ids"] = []
    return {"segments": segments}


class SegmentPatch(BaseModel):
    title: str | None = None
    summary: str | None = None
    topics: list[str] | None = None


@app.patch("/api/segments/{seg_id}")
def patch_segment(seg_id: int, body: SegmentPatch):
    import json as _json
    from datetime import datetime, timezone
    conn = db.connect()
    row = conn.execute("SELECT id FROM segments WHERE id=?", (seg_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="segment not found")
    updates: dict = {}
    if body.title is not None:
        updates["title"] = body.title[:500]
    if body.summary is not None:
        updates["summary"] = body.summary[:5000]
    if body.topics is not None:
        updates["topics"] = _json.dumps(body.topics, ensure_ascii=False)
    if not updates:
        raise HTTPException(status_code=400, detail="no fields to update")
    updates["locked_by_user"] = 1
    updates["user_edited_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    set_clause = ", ".join(f"{k}=?" for k in updates)
    with conn:
        conn.execute(
            f"UPDATE segments SET {set_clause} WHERE id=?",
            list(updates.values()) + [seg_id],
        )
    return {"ok": True}


@app.delete("/api/segments/{seg_id}")
def delete_segment(seg_id: int):
    conn = db.connect()
    row = conn.execute("SELECT id FROM segments WHERE id=?", (seg_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="segment not found")
    with conn:
        conn.execute("DELETE FROM segments WHERE id=?", (seg_id,))
    return {"ok": True}


class AssertionPatch(BaseModel):
    text: str | None = None
    actor: str | None = None
    kind: str | None = None
    status: str | None = None
    confidence: float | None = None


_VALID_ACTORS = {"user", "assistant", "shared"}
_VALID_KINDS = {"claim", "hypothesis", "conclusion", "decision",
                "rejected_idea", "question", "todo"}
_VALID_STATUSES = {"tentative", "accepted", "rejected", "superseded",
                   "unresolved", "completed"}


@app.patch("/api/assertions/{assertion_id}")
def patch_assertion(assertion_id: int, body: AssertionPatch):
    from datetime import datetime, timezone
    conn = db.connect()
    row = conn.execute("SELECT id FROM assertions WHERE id=?", (assertion_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="assertion not found")
    updates: dict = {}
    if body.text is not None:
        if not body.text.strip():
            raise HTTPException(status_code=422, detail="text cannot be empty")
        updates["text"] = body.text[:2000]
    if body.actor is not None:
        if body.actor not in _VALID_ACTORS:
            raise HTTPException(status_code=422, detail=f"actor must be one of {sorted(_VALID_ACTORS)}")
        updates["actor"] = body.actor
    if body.kind is not None:
        if body.kind not in _VALID_KINDS:
            raise HTTPException(status_code=422, detail=f"kind must be one of {sorted(_VALID_KINDS)}")
        updates["kind"] = body.kind
    if body.status is not None:
        if body.status not in _VALID_STATUSES:
            raise HTTPException(status_code=422, detail=f"status must be one of {sorted(_VALID_STATUSES)}")
        updates["status"] = body.status
    if body.confidence is not None:
        if not (0.0 <= body.confidence <= 1.0):
            raise HTTPException(status_code=422, detail="confidence must be 0.0–1.0")
        updates["confidence"] = body.confidence
    if not updates:
        raise HTTPException(status_code=400, detail="no fields to update")
    updates["locked_by_user"] = 1
    updates["user_edited_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    set_clause = ", ".join(f"{k}=?" for k in updates)
    with conn:
        conn.execute(
            f"UPDATE assertions SET {set_clause} WHERE id=?",
            list(updates.values()) + [assertion_id],
        )
    return {"ok": True}


@app.delete("/api/assertions/{assertion_id}")
def delete_assertion(assertion_id: int):
    conn = db.connect()
    row = conn.execute("SELECT id FROM assertions WHERE id=?", (assertion_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="assertion not found")
    with conn:
        conn.execute("DELETE FROM assertions WHERE id=?", (assertion_id,))
    return {"ok": True}


# Serve the built frontend if present (production mode: single process).
_dist = os.path.join(os.path.dirname(__file__), "..", "..", "frontend", "dist")
if os.path.isdir(_dist):
    app.mount("/", StaticFiles(directory=_dist, html=True), name="frontend")
