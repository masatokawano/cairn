"""End-to-end tests for the one-time redaction migration (app.admin).

Simulates a pre-redaction (Phase 1) DB by inserting plaintext secrets
directly with SQL, bypassing the ingest-time redaction. All secrets are
format-matching DUMMY values.
"""
import importlib
import json
import os

import pytest

FAKE_OPENAI = "sk-proj-LEGACYDUMMY000000000000000000000000LEGACYDUMMY"
FAKE_AWS = "AKIAIOSFODNN7EXAMPLE"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("CAIRN_DB", str(tmp_path / "cairn.db"))
    monkeypatch.setenv("CAIRN_CLAUDE_DIR", str(tmp_path / "claude_logs"))
    monkeypatch.setenv("CAIRN_CODEX_DIR", str(tmp_path / "codex_logs"))
    from app import admin, cli_sync, db
    importlib.reload(db)
    importlib.reload(cli_sync)
    importlib.reload(admin)
    yield {"db": db, "admin": admin, "cli_sync": cli_sync, "tmp": tmp_path}
    conn = getattr(db._local, "conn", None)
    if conn:
        conn.close()
        db._local.conn = None


def seed_legacy_rows(db):
    """Insert plaintext rows the way Phase 1 would have stored them."""
    from app.parsers.base import ParsedConversation, ParsedMessage

    conn = db.connect()
    legacy = ParsedConversation(
        source="codex_cli", source_id="legacy-1", title="キー設定の相談",
        messages=[
            ParsedMessage(role="user", text=f"このキーで {FAKE_OPENAI} 設定して", created_at="2025-12-11T09:01:00Z"),
            ParsedMessage(role="assistant", text="できません", created_at="2025-12-11T09:01:30Z"),
            ParsedMessage(role="user", text=f"AWSは {FAKE_AWS} です", created_at="2025-12-11T09:02:00Z"),
        ],
    )
    with conn:
        cur = conn.execute(
            "INSERT INTO conversations (source, source_id, title, created_at, updated_at, content_hash, meta)"
            " VALUES (?,?,?,?,?,?,?)",
            (legacy.source, legacy.source_id, legacy.title,
             "2025-12-11T09:01:00Z", "2025-12-11T09:02:00Z",
             legacy.content_hash(),  # hash of PLAINTEXT, as Phase 1 stored it
             json.dumps({})),
        )
        conn.executemany(
            "INSERT INTO messages (conversation_id, idx, role, text, created_at) VALUES (?,?,?,?,?)",
            [(cur.lastrowid, i, m.role, m.text, m.created_at) for i, m in enumerate(legacy.messages)],
        )
    return legacy


def test_scan_reports_counts(env):
    db, admin = env["db"], env["admin"]
    seed_legacy_rows(db)
    report = admin._scan_db()
    assert report["providers"] == {"openai": 1, "aws": 1}
    assert report["affected_messages"] == 2
    assert report["affected_conversations"] == 1


def test_apply_redacts_everything(env, capsys):
    db, admin = env["db"], env["admin"]
    seed_legacy_rows(db)
    db_path = os.path.abspath(db.DB_PATH)

    # secret is searchable before (proves the test setup is real)
    assert len(db.search(FAKE_OPENAI[:20])) == 1

    rc = admin.main(["redact-apply", "--yes"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "verification OK" in out

    # 1. rows redacted
    conv = db.get_conversation(1)
    texts = "\n".join(m["text"] for m in conv["messages"])
    assert FAKE_OPENAI not in texts and FAKE_AWS not in texts
    assert "[REDACTED:openai]" in texts and "[REDACTED:aws]" in texts

    # 2. FTS consistent: secret unfindable, marker findable, clean text still findable
    assert db.search(FAKE_OPENAI[:20]) == []
    assert len(db.search("REDACTED")) == 1
    assert len(db.search("できません")) == 1

    # 3. content_hash recomputed to match redacted text
    row = db.connect().execute("SELECT content_hash FROM conversations WHERE id=1").fetchone()
    from app.parsers.base import ParsedConversation, ParsedMessage
    expected = ParsedConversation(
        source="", source_id="", title="",
        messages=[ParsedMessage(role=m["role"], text=m["text"], created_at=m["created_at"])
                  for m in conv["messages"]],
    ).content_hash()
    assert row["content_hash"] == expected

    # 4. no plaintext in raw DB/WAL/SHM bytes
    for suffix in ("", "-wal", "-shm"):
        path = db_path + suffix
        if os.path.exists(path):
            with open(path, "rb") as f:
                blob = f.read()
            assert FAKE_OPENAI.encode() not in blob, f"plaintext in {path}"
            assert FAKE_AWS.encode() not in blob, f"plaintext in {path}"

    # 5. a backup was created (and intentionally still has the plaintext)
    backups = [p for p in os.listdir(os.path.dirname(db_path)) if ".backup-" in p]
    assert len(backups) == 1


def test_resync_does_not_revive_secrets(env):
    """Re-parsing the original log must not bring plaintext back:
    ingest-time redaction produces the same redacted content → skip."""
    db, admin, cli_sync = env["db"], env["admin"], env["cli_sync"]
    tmp = env["tmp"]

    # A claude CLI log file containing the dummy secret
    log_dir = tmp / "claude_logs" / "-Users-test-proj"
    log_dir.mkdir(parents=True)
    lines = [
        json.dumps({"type": "user", "isSidechain": False, "uuid": "u1", "sessionId": "legacy-1",
                    "timestamp": "2025-12-11T09:01:00Z", "cwd": "/Users/test/proj",
                    "message": {"role": "user", "content": f"このキーで {FAKE_OPENAI} 設定して"}}),
        json.dumps({"type": "assistant", "isSidechain": False, "uuid": "u2", "sessionId": "legacy-1",
                    "timestamp": "2025-12-11T09:01:30Z",
                    "message": {"role": "assistant", "content": [{"type": "text", "text": "できません"}]}}),
    ]
    (log_dir / "legacy-1.jsonl").write_text("\n".join(lines))

    # First sync ingests (redacted at ingest)
    stats = cli_sync.scan_once()
    assert stats["inserted"] == 1
    conv = db.get_conversation(1)
    assert FAKE_OPENAI not in conv["messages"][0]["text"]

    # Force a real re-parse: clear ingest_files state so the file is re-read
    conn = db.connect()
    with conn:
        conn.execute("DELETE FROM ingest_files")
    stats = cli_sync.scan_once()
    assert stats["files_imported"] == 1
    assert stats["skipped"] == 1  # identical redacted content → diff import skips
    conv = db.get_conversation(1)
    assert FAKE_OPENAI not in conv["messages"][0]["text"]
    assert db.search(FAKE_OPENAI[:20]) == []


def test_apply_on_clean_db_is_noop(env, capsys):
    db, admin = env["db"], env["admin"]
    from app.parsers.base import ParsedConversation, ParsedMessage
    db.upsert_conversations([ParsedConversation(
        source="chatgpt", source_id="c1", title="clean",
        messages=[ParsedMessage(role="user", text="秘密情報なし")],
    )])
    rc = admin.main(["redact-apply", "--yes"])
    assert rc == 0
    assert "nothing to redact" in capsys.readouterr().out
