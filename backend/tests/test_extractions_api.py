"""API tests for P3-E: extractions endpoints (GET/PATCH/DELETE segments + assertions)."""
import importlib
import json

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
    yield TestClient(main.app, base_url="http://127.0.0.1")
    conn = getattr(db._local, "conn", None)
    if conn:
        conn.close()
        db._local.conn = None


@pytest.fixture()
def seeded(tmp_path, monkeypatch):
    """Returns (client, db_module, conv_id, seg_id, assertion_id)."""
    monkeypatch.setenv("CAIRN_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("CAIRN_CLAUDE_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("CAIRN_CODEX_DIR", str(tmp_path / "codex"))
    from app import cli_sync, db as db_module, main
    importlib.reload(db_module)
    importlib.reload(cli_sync)
    importlib.reload(main)

    from app.parsers.base import ParsedConversation, ParsedMessage
    conv = ParsedConversation(
        source="chatgpt", source_id="c-api-test", title="API test conv",
        messages=[
            ParsedMessage(role="user", text="Hello", created_at="2025-01-01T00:00:00Z"),
            ParsedMessage(role="assistant", text="Hi there", created_at="2025-01-01T00:01:00Z"),
        ],
        created_at="2025-01-01T00:00:00Z", updated_at="2025-01-01T00:01:00Z",
    )
    db_module.upsert_conversations([conv])
    conn = db_module.connect()
    conv_id = conn.execute("SELECT id FROM conversations LIMIT 1").fetchone()[0]
    msg_ids = [r[0] for r in conn.execute(
        "SELECT id FROM messages WHERE conversation_id=? ORDER BY id", (conv_id,)
    ).fetchall()]

    seg_id = db_module.insert_segment(
        conversation_id=conv_id, idx=0,
        start_message_id=msg_ids[0], end_message_id=msg_ids[-1],
        title="Test seg", summary="A summary.",
        generated_by="fixture:v1", prompt_version="segment-v1",
        created_at="2026-01-01T00:00:00",
    )
    assertion_id = db_module.insert_assertion(
        segment_id=seg_id, conversation_id=conv_id,
        text="The system uses SQLite.", actor="assistant", kind="claim",
        status="tentative", confidence=0.9,
        supporting_message_ids=json.dumps([msg_ids[0]]),
        generated_by="fixture:v1", prompt_version="assertion-v1",
        created_at="2026-01-01T00:00:00",
    )

    cli = TestClient(main.app, base_url="http://127.0.0.1")
    yield cli, db_module, conv_id, seg_id, assertion_id
    conn2 = getattr(db_module._local, "conn", None)
    if conn2:
        conn2.close()
        db_module._local.conn = None


# ---------------------------------------------------------------------------
# GET /api/conversations/{id}/extractions
# ---------------------------------------------------------------------------

def test_get_extractions_returns_segments_and_assertions(seeded):
    cli, db_module, conv_id, seg_id, assertion_id = seeded
    r = cli.get(f"/api/conversations/{conv_id}/extractions")
    assert r.status_code == 200
    data = r.json()
    assert "segments" in data
    assert len(data["segments"]) == 1
    seg = data["segments"][0]
    assert seg["title"] == "Test seg"
    assert len(seg["assertions"]) == 1
    a = seg["assertions"][0]
    assert a["text"] == "The system uses SQLite."
    assert isinstance(a["supporting_message_ids"], list)


def test_get_extractions_404(client):
    r = client.get("/api/conversations/9999/extractions")
    assert r.status_code == 404


def test_get_extractions_empty(seeded):
    cli, db_module, conv_id, seg_id, assertion_id = seeded
    # Delete assertions; segments should still be returned with empty list.
    db_module.connect().execute("DELETE FROM assertions")
    db_module.connect().commit()
    r = cli.get(f"/api/conversations/{conv_id}/extractions")
    assert r.status_code == 200
    assert r.json()["segments"][0]["assertions"] == []


# ---------------------------------------------------------------------------
# PATCH /api/segments/{id}
# ---------------------------------------------------------------------------

def test_patch_segment_title(seeded):
    cli, db_module, conv_id, seg_id, _ = seeded
    r = cli.patch(f"/api/segments/{seg_id}", json={"title": "New title"})
    assert r.status_code == 200
    row = db_module.connect().execute("SELECT title, locked_by_user, user_edited_at FROM segments WHERE id=?", (seg_id,)).fetchone()
    assert row["title"] == "New title"
    assert row["locked_by_user"] == 1
    assert row["user_edited_at"] is not None


def test_patch_segment_summary_and_topics(seeded):
    cli, db_module, conv_id, seg_id, _ = seeded
    r = cli.patch(f"/api/segments/{seg_id}", json={"summary": "Better summary.", "topics": ["a", "b"]})
    assert r.status_code == 200
    row = db_module.connect().execute("SELECT summary, topics FROM segments WHERE id=?", (seg_id,)).fetchone()
    assert row["summary"] == "Better summary."
    assert json.loads(row["topics"]) == ["a", "b"]


def test_patch_segment_locks_row(seeded):
    cli, db_module, conv_id, seg_id, _ = seeded
    cli.patch(f"/api/segments/{seg_id}", json={"title": "x"})
    row = db_module.connect().execute("SELECT locked_by_user FROM segments WHERE id=?", (seg_id,)).fetchone()
    assert row["locked_by_user"] == 1


def test_patch_segment_404(seeded):
    cli, *_ = seeded
    r = cli.patch("/api/segments/9999", json={"title": "x"})
    assert r.status_code == 404


def test_patch_segment_no_fields(seeded):
    cli, db_module, conv_id, seg_id, _ = seeded
    r = cli.patch(f"/api/segments/{seg_id}", json={})
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# DELETE /api/segments/{id}
# ---------------------------------------------------------------------------

def test_delete_segment(seeded):
    cli, db_module, conv_id, seg_id, _ = seeded
    r = cli.delete(f"/api/segments/{seg_id}")
    assert r.status_code == 200
    row = db_module.connect().execute("SELECT id FROM segments WHERE id=?", (seg_id,)).fetchone()
    assert row is None
    # Cascading: assertions should also be gone.
    rows = db_module.connect().execute("SELECT id FROM assertions WHERE segment_id=?", (seg_id,)).fetchall()
    assert rows == []


def test_delete_segment_404(seeded):
    cli, *_ = seeded
    r = cli.delete("/api/segments/9999")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# PATCH /api/assertions/{id}
# ---------------------------------------------------------------------------

def test_patch_assertion_text(seeded):
    cli, db_module, conv_id, seg_id, assertion_id = seeded
    r = cli.patch(f"/api/assertions/{assertion_id}", json={"text": "Updated claim."})
    assert r.status_code == 200
    row = db_module.connect().execute("SELECT text, locked_by_user FROM assertions WHERE id=?", (assertion_id,)).fetchone()
    assert row["text"] == "Updated claim."
    assert row["locked_by_user"] == 1


def test_patch_assertion_actor_kind_status(seeded):
    cli, db_module, conv_id, seg_id, assertion_id = seeded
    r = cli.patch(f"/api/assertions/{assertion_id}",
                  json={"actor": "user", "kind": "decision", "status": "accepted"})
    assert r.status_code == 200
    row = db_module.connect().execute(
        "SELECT actor, kind, status FROM assertions WHERE id=?", (assertion_id,)
    ).fetchone()
    assert row["actor"] == "user"
    assert row["kind"] == "decision"
    assert row["status"] == "accepted"


def test_patch_assertion_invalid_actor(seeded):
    cli, *_ = seeded
    r = cli.patch(f"/api/assertions/{_[2]}", json={"actor": "robot"})
    assert r.status_code == 422


def test_patch_assertion_invalid_kind(seeded):
    cli, *_ = seeded
    r = cli.patch(f"/api/assertions/{_[2]}", json={"kind": "rant"})
    assert r.status_code == 422


def test_patch_assertion_invalid_status(seeded):
    cli, *_ = seeded
    r = cli.patch(f"/api/assertions/{_[2]}", json={"status": "maybe"})
    assert r.status_code == 422


def test_patch_assertion_empty_text(seeded):
    cli, *_ = seeded
    r = cli.patch(f"/api/assertions/{_[2]}", json={"text": "  "})
    assert r.status_code == 422


def test_patch_assertion_404(seeded):
    cli, *_ = seeded
    r = cli.patch("/api/assertions/9999", json={"text": "x"})
    assert r.status_code == 404


def test_patch_assertion_no_fields(seeded):
    cli, *_ = seeded
    r = cli.patch(f"/api/assertions/{_[2]}", json={})
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# DELETE /api/assertions/{id}
# ---------------------------------------------------------------------------

def test_delete_assertion(seeded):
    cli, db_module, conv_id, seg_id, assertion_id = seeded
    r = cli.delete(f"/api/assertions/{assertion_id}")
    assert r.status_code == 200
    row = db_module.connect().execute("SELECT id FROM assertions WHERE id=?", (assertion_id,)).fetchone()
    assert row is None
    # Segment should still exist.
    seg = db_module.connect().execute("SELECT id FROM segments WHERE id=?", (seg_id,)).fetchone()
    assert seg is not None


def test_delete_assertion_404(seeded):
    cli, *_ = seeded
    r = cli.delete("/api/assertions/9999")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Locked row protection: edit → re-extract does not overwrite
# ---------------------------------------------------------------------------

def test_locked_segment_not_overwritten_by_force(seeded):
    """PATCH locks the segment; subsequent --force extract preserves it."""
    cli, db_module, conv_id, seg_id, _ = seeded
    cli.patch(f"/api/segments/{seg_id}", json={"title": "Manual title"})

    # Simulate force re-extraction: delete_unlocked_segments should skip locked row.
    deleted = db_module.delete_unlocked_segments(conv_id)
    assert deleted == 0  # locked row must survive
    row = db_module.connect().execute("SELECT title FROM segments WHERE id=?", (seg_id,)).fetchone()
    assert row["title"] == "Manual title"


def test_locked_assertion_not_overwritten_by_force(seeded):
    """PATCH locks the assertion; subsequent delete_unlocked_assertions skips it."""
    cli, db_module, conv_id, seg_id, assertion_id = seeded
    cli.patch(f"/api/assertions/{assertion_id}", json={"text": "Manual text."})

    deleted = db_module.delete_unlocked_assertions(seg_id)
    assert deleted == 0
    row = db_module.connect().execute("SELECT text FROM assertions WHERE id=?", (assertion_id,)).fetchone()
    assert row["text"] == "Manual text."
