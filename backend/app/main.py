"""Cairn API server. Serves /api/* and the built frontend (frontend/dist)."""
from __future__ import annotations

import logging
import os

from fastapi import FastAPI, HTTPException, Query, UploadFile
from fastapi.staticfiles import StaticFiles

from . import cli_sync, db
from .parsers import UnknownFormatError, parse_upload

logging.basicConfig(level=logging.INFO)

app = FastAPI(title="Cairn")


@app.on_event("startup")
def startup():
    db.connect()
    cli_sync.start_background_sync()


@app.post("/api/import")
async def import_file(file: UploadFile):
    raw = await file.read()
    try:
        result = parse_upload(file.filename or "upload.json", raw)
    except UnknownFormatError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=f"パースに失敗しました: {e}")
    stats = db.upsert_conversations(result.conversations)
    return {
        "filename": file.filename,
        "conversations": len(result.conversations),
        **stats,
        "warnings": result.warnings[:20],
    }


@app.post("/api/sync")
def sync_now():
    return cli_sync.scan_once()


@app.get("/api/search")
def search(
    q: str = Query(..., min_length=1),
    source: str | None = None,
    limit: int = Query(50, le=200),
    offset: int = 0,
):
    return {"query": q, "results": db.search(q, source=source, limit=limit, offset=offset)}


@app.get("/api/conversations")
def conversations(source: str | None = None, limit: int = Query(100, le=500), offset: int = 0):
    return {"results": db.list_conversations(source=source, limit=limit, offset=offset)}


@app.get("/api/conversations/{conv_id}")
def conversation(conv_id: int):
    conv = db.get_conversation(conv_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    return conv


@app.get("/api/stats")
def stats():
    return db.stats()


# Serve the built frontend if present (production mode: single process).
_dist = os.path.join(os.path.dirname(__file__), "..", "..", "frontend", "dist")
if os.path.isdir(_dist):
    app.mount("/", StaticFiles(directory=_dist, html=True), name="frontend")
