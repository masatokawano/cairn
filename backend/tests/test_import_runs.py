"""Tests for import run history (P1-B): every ingest of a single input
records one import_runs row, viewable via the API and admin CLI."""
import glob
import importlib
import json
import os

import pytest
from fastapi.testclient import TestClient

from tests.schema_shapes import downgrade_chunks_pre_v11

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CAIRN_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("CAIRN_CLAUDE_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("CAIRN_CODEX_DIR", str(tmp_path / "codex"))
    from app import cli_sync, db, main
    importlib.reload(db)
    importlib.reload(cli_sync)
    importlib.reload(main)
    yield TestClient(main.app, base_url="http://127.0.0.1"), db, cli_sync
    conn = getattr(db._local, "conn", None)
    if conn:
        conn.close()
        db._local.conn = None


def _chatgpt_bytes():
    with open(os.path.join(FIXTURES, "chatgpt_sample.json"), "rb") as f:
        return f.read()


def test_upload_records_ok_run(client):
    c, db, _ = client
    resp = c.post("/api/import", files={"file": ("conversations.json", _chatgpt_bytes())})
    assert resp.status_code == 200
    runs = db.list_import_runs()
    assert len(runs) == 1
    run = runs[0]
    assert run["source"] == "upload"
    assert run["input_name"] == "conversations.json"
    assert run["status"] == "ok"
    assert run["parser_version"]
    assert run["inserted"] >= 1
    assert run["completed_at"] and run["content_hash"]


def test_upload_unknown_format_records_error_run(client):
    c, db, _ = client
    resp = c.post("/api/import", files={"file": ("x.json", b'{"not": "a known format"}')})
    assert resp.status_code == 422
    runs = db.list_import_runs()
    assert len(runs) == 1
    assert runs[0]["status"] == "error"
    assert runs[0]["error"]
    assert runs[0]["inserted"] == 0


def test_api_endpoint_lists_runs_newest_first(client):
    c, db, _ = client
    c.post("/api/import", files={"file": ("conversations.json", _chatgpt_bytes())})
    c.post("/api/import", files={"file": ("x.json", b"{}")})  # error run
    resp = c.get("/api/import-runs")
    assert resp.status_code == 200
    results = resp.json()["results"]
    assert len(results) == 2
    assert results[0]["id"] > results[1]["id"]  # DESC order
    # source filter
    ok = c.get("/api/import-runs", params={"source": "upload"}).json()["results"]
    assert all(r["source"] == "upload" for r in ok)


def test_cli_sync_records_per_file_runs(client, tmp_path):
    c, db, cli_sync = client
    log_dir = tmp_path / "claude" / "-Users-test-proj"
    log_dir.mkdir(parents=True)
    lines = [
        json.dumps({"type": "user", "isSidechain": False, "uuid": "u1",
                    "sessionId": "sess-1", "timestamp": "2025-12-11T09:01:00Z",
                    "cwd": "/Users/test/proj",
                    "message": {"role": "user", "content": "テスト質問"}}),
        json.dumps({"type": "assistant", "isSidechain": False, "uuid": "u2",
                    "sessionId": "sess-1", "timestamp": "2025-12-11T09:01:30Z",
                    "message": {"role": "assistant", "content": [{"type": "text", "text": "回答"}]}}),
    ]
    (log_dir / "sess-1.jsonl").write_text("\n".join(lines))

    stats = cli_sync.scan_once()
    assert stats["files_imported"] == 1
    runs = db.list_import_runs(source="claude_cli")
    assert len(runs) == 1
    assert runs[0]["source"] == "claude_cli"
    assert runs[0]["input_name"].endswith("sess-1.jsonl")
    assert runs[0]["inserted"] == 1
    assert runs[0]["content_hash"]


def test_admin_import_runs_command(client, capsys):
    c, db, _ = client
    c.post("/api/import", files={"file": ("conversations.json", _chatgpt_bytes())})
    from app import admin
    importlib.reload(admin)
    rc = admin.main(["import-runs", "--limit", "5"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert len(out) == 1 and out[0]["source"] == "upload"


def test_migration_creates_import_runs_on_pre_v2_db(client, tmp_path):
    """An existing v1 DB (no import_runs) is upgraded to v2 with the table
    present and a pre-migration backup taken."""
    c, db, _ = client
    db.connect()
    # Simulate a genuine pre-v2 DB: no import_runs table and no later columns,
    # version rolled back to 1 (so migrations v2 through v11 all run on reopen).
    # A pre-v2 shape has none of the v11 items registry either — drop those
    # artefacts too so migration 11's non-idempotent ALTER TABLE does not
    # collide with the pre-existing item_id column on the fresh build.
    conn = db.connect()
    downgrade_chunks_pre_v11(conn)
    with conn:
        conn.execute("DROP TABLE import_runs")
        conn.execute("ALTER TABLE messages DROP COLUMN source_message_id")
        conn.execute("PRAGMA user_version = 1")
    conn.close()
    db._local.conn = None

    # Reopen → migration runs.
    conn = db.connect()
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db._SCHEMA_VERSION
    assert conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='import_runs'"
    ).fetchone()[0] == 1
    assert glob.glob(str(tmp_path / "*.premigrate-v1-to-*"))
