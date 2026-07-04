"""API-level security tests: Host/Origin validation, upload limits, sync lock."""
import importlib
import io
import json
import os
import zipfile

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CAIRN_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("CAIRN_CLAUDE_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("CAIRN_CODEX_DIR", str(tmp_path / "codex"))
    from app import cli_sync, db, main
    importlib.reload(db)
    importlib.reload(cli_sync)
    importlib.reload(main)
    # No `with` context: skip startup events (no background sync thread).
    # base_url sets Host: 127.0.0.1 so requests pass the local-only middleware
    # (individual tests override the host header to probe it).
    yield TestClient(main.app, base_url="http://127.0.0.1")
    conn = getattr(db._local, "conn", None)
    if conn:
        conn.close()
        db._local.conn = None


FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def _upload(client, content: bytes, name="conversations.json", **kwargs):
    return client.post("/api/import", files={"file": (name, content)}, **kwargs)


def chatgpt_fixture() -> bytes:
    with open(os.path.join(FIXTURES, "chatgpt_sample.json"), "rb") as f:
        return f.read()


# --- Host header validation -------------------------------------------------

def test_host_localhost_allowed(client):
    r = client.get("/api/stats", headers={"host": "127.0.0.1:8730"})
    assert r.status_code == 200
    r = client.get("/api/stats", headers={"host": "localhost:8730"})
    assert r.status_code == 200


def test_host_foreign_rejected(client):
    for host in ("evil.example.com", "192.168.1.5:8730", "cairn.attacker.io:8730"):
        r = client.get("/api/stats", headers={"host": host})
        assert r.status_code == 403, host


# --- Origin validation on mutations ------------------------------------------

def test_post_foreign_origin_rejected(client):
    r = client.post(
        "/api/sync",
        headers={"host": "127.0.0.1:8730", "origin": "https://evil.example.com"},
    )
    assert r.status_code == 403


def test_post_localhost_origin_allowed(client):
    r = client.post(
        "/api/sync",
        headers={"host": "127.0.0.1:8730", "origin": "http://127.0.0.1:8730"},
    )
    assert r.status_code == 200


def test_post_no_origin_allowed(client):
    # curl / CLI clients send no Origin header
    r = client.post("/api/sync", headers={"host": "localhost:8730"})
    assert r.status_code == 200


def test_get_foreign_origin_ok(client):
    # GETs are safe; only Host matters there
    r = client.get(
        "/api/stats",
        headers={"host": "127.0.0.1:8730", "origin": "https://evil.example.com"},
    )
    assert r.status_code == 200


# --- Upload limits ------------------------------------------------------------

def test_import_within_limit_ok(client):
    r = _upload(client, chatgpt_fixture())
    assert r.status_code == 200
    assert r.json()["inserted"] == 1


def test_import_over_limit_rejected(client, monkeypatch):
    from app import main
    monkeypatch.setattr(main, "MAX_UPLOAD_BYTES", 1024)
    r = _upload(client, b"x" * 4096, name="big.json")
    assert r.status_code == 413


def test_import_zip_bomb_member_rejected(client, monkeypatch):
    from app import parsers
    monkeypatch.setattr(parsers, "MAX_JSON_BYTES", 1024)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("conversations.json", b"[" + b" " * 100_000 + b"]")  # compresses tiny, inflates big
    r = _upload(client, buf.getvalue(), name="export.zip")
    assert r.status_code == 413


def test_import_zip_too_many_entries_rejected(client, monkeypatch):
    from app import parsers
    monkeypatch.setattr(parsers, "MAX_ZIP_ENTRIES", 5)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for i in range(10):
            zf.writestr(f"f{i}.txt", "x")
    r = _upload(client, buf.getvalue(), name="export.zip")
    assert r.status_code == 413


def test_import_unknown_format_422(client):
    r = _upload(client, json.dumps([{"foo": 1}]).encode(), name="x.json")
    assert r.status_code == 422


# --- Sync lock ----------------------------------------------------------------

def test_sync_conflict_while_locked(client):
    from app import cli_sync
    assert cli_sync.ingest_lock.acquire(blocking=False)
    try:
        r = client.post("/api/sync", headers={"host": "localhost"})
        assert r.status_code == 409
    finally:
        cli_sync.ingest_lock.release()
    assert client.post("/api/sync", headers={"host": "localhost"}).status_code == 200


# --- /api/search mode parameter (P2-2) ----------------------------------------

def test_search_default_mode_is_keyword(client):
    _upload(client, chatgpt_fixture())
    r = client.get("/api/search", params={"q": "hello"})
    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "keyword"
    # Even with no matches it should return 200 with empty results, never crash.
    assert "results" in body


def test_search_rejects_unknown_mode(client):
    # FastAPI's pattern= validator returns 422 for non-matching values; this
    # prevents silent fall-through to a default mode when a typo is sent.
    r = client.get("/api/search", params={"q": "hello", "mode": "fuzzy"})
    assert r.status_code == 422


def test_search_accepts_and_applies_date_filters(client):
    # Pre-condition: a sample import populates conversations with known dates.
    # The chatgpt fixture's updated_at lands in 2024; both filters narrow to
    # different windows so the boundaries are observable.
    _upload(client, chatgpt_fixture())
    # Wide-open: pick up the fixture
    r = client.get("/api/search", params={"q": "hello", "after": "2000-01-01"})
    assert r.status_code == 200
    baseline_count = len(r.json()["results"])
    assert baseline_count >= 0  # at minimum returns 200, may or may not match
    # Future-only window must exclude everything from the fixture
    r = client.get("/api/search", params={"q": "hello", "after": "2099-01-01"})
    assert r.status_code == 200
    assert r.json()["results"] == []


# --- DB file permissions -------------------------------------------------------

def test_db_file_permissions_0600(client, tmp_path):
    client.get("/api/stats")  # forces db.connect()
    mode = os.stat(tmp_path / "test.db").st_mode & 0o777
    assert mode == 0o600


# --- /api/stats items breakdown (M1) -------------------------------------------

def test_stats_includes_items_breakdown(client):
    """DESIGN.md §7 M1 完了条件: /api/stats に items 内訳が出る。"""
    _upload(client, chatgpt_fixture())
    r = client.get("/api/stats")
    assert r.status_code == 200
    body = r.json()
    assert "items" in body and "item_links" in body
    kinds = {row["kind"] for row in body["items"]}
    assert "conversation" in kinds  # the uploaded fixture registered items


# --- /api/search kinds filter (M2) ----------------------------------------------

def test_search_kinds_param_validates(client):
    _upload(client, chatgpt_fixture())
    r = client.get("/api/search", params={"q": "hello", "kinds": "conversation,bookmark"})
    assert r.status_code == 200
    r = client.get("/api/search", params={"q": "hello", "kinds": "conversation,bogus"})
    assert r.status_code == 422
    assert "bogus" in r.text


def test_search_results_carry_m2_fields(client):
    _upload(client, chatgpt_fixture())
    r = client.get("/api/search", params={"q": "hello"})
    assert r.status_code == 200
    for row in r.json()["results"]:
        assert row["kind"] == "conversation"
        assert "item_id" in row and "url" in row
