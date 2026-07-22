"""Tests for GET /api/export (backlog A5): the same filtered export the
admin CLI produces (export-jsonl / export-markdown), streamed over HTTP.
db.iter_export_jsonl / iter_export_markdown are the shared generators —
these tests exercise the HTTP layer; tests/test_export.py and
tests/test_export_markdown.py cover the record-shaping in depth and must
keep passing unmodified (proof the refactor is behavior-preserving)."""
import importlib
import json
import os

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CAIRN_DB", str(tmp_path / "test.db"))
    from app import db, main
    importlib.reload(db)
    importlib.reload(main)
    yield TestClient(main.app, base_url="http://127.0.0.1"), db
    conn = getattr(db._local, "conn", None)
    if conn:
        conn.close()
        db._local.conn = None


def make_conv(source="chatgpt", source_id="c1", title="エクスポート対象"):
    from app.parsers.base import ParsedConversation, ParsedMessage
    return ParsedConversation(
        source=source, source_id=source_id, title=title,
        messages=[
            ParsedMessage(role="user", text="質問本文", created_at="2025-01-01T00:00:00Z"),
            ParsedMessage(role="assistant", text="回答本文", created_at="2025-01-01T00:01:00Z"),
        ],
        created_at="2025-01-01T00:00:00Z", updated_at="2025-01-01T00:10:00Z",
    )


def test_export_jsonl_endpoint_streams_filtered_conversations(client):
    c, db = client
    db.upsert_conversations([make_conv()])
    resp = c.get("/api/export", params={"format": "jsonl"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/x-ndjson")
    assert "attachment; filename=" in resp.headers["content-disposition"]
    lines = [l for l in resp.text.splitlines() if l]
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["schema"] == "cairn.export.v1"
    assert record["source"] == "chatgpt"
    assert record["title"] == "エクスポート対象"
    assert [m["text"] for m in record["messages"]] == ["質問本文", "回答本文"]


def test_export_markdown_endpoint_renders_sections(client):
    c, db = client
    db.upsert_conversations([make_conv()])
    resp = c.get("/api/export", params={"format": "markdown"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/markdown")
    assert "attachment; filename=" in resp.headers["content-disposition"]
    body = resp.text
    assert "# エクスポート対象" in body
    assert "- source: chatgpt" in body
    assert "## user" in body and "質問本文" in body
    assert "## assistant" in body and "回答本文" in body


def test_export_rejects_unknown_format(client):
    c, _db = client
    resp = c.get("/api/export", params={"format": "xml"})
    assert resp.status_code == 422


def test_export_source_filter_excludes_other_sources(client):
    c, db = client
    db.upsert_conversations([
        make_conv(source="chatgpt", source_id="c1", title="ChatGPT分"),
        make_conv(source="claude", source_id="c2", title="Claude分"),
    ])
    resp = c.get("/api/export", params={"format": "jsonl", "source": "claude"})
    assert resp.status_code == 200
    lines = [json.loads(l) for l in resp.text.splitlines() if l]
    assert len(lines) == 1
    assert lines[0]["source"] == "claude"
    assert lines[0]["title"] == "Claude分"
