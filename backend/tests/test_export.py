"""Export JSONL tests (P1-F): the export must be machine-readable and able
to reconstruct conversation body / dates / source, with source / date range /
conversation_id filters working as documented in docs/architecture-audit.md.
"""
import importlib
import io
import json
import os

import pytest


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("CAIRN_DB", str(tmp_path / "test.db"))
    from app import db as db_module
    importlib.reload(db_module)
    yield db_module
    conn = getattr(db_module._local, "conn", None)
    if conn:
        conn.close()
        db_module._local.conn = None


def make_conv(source="chatgpt", source_id="c1", updated="2025-01-01T00:10:00Z",
              title="エクスポート対象", texts=None, msg_ids=None):
    from app.parsers.base import ParsedConversation, ParsedMessage
    texts = texts or [("user", "質問"), ("assistant", "回答")]
    msg_ids = msg_ids or [None] * len(texts)
    return ParsedConversation(
        source=source, source_id=source_id, title=title,
        messages=[
            ParsedMessage(role=r, text=t, created_at="2025-01-01T00:00:00Z", source_message_id=mid)
            for (r, t), mid in zip(texts, msg_ids)
        ],
        created_at="2025-01-01T00:00:00Z", updated_at=updated,
    )


def _dump(db, **filters) -> list[dict]:
    buf = io.StringIO()
    n = db.export_jsonl(buf, **filters)
    lines = [ln for ln in buf.getvalue().splitlines() if ln]
    assert n == len(lines)
    return [json.loads(ln) for ln in lines]


def test_jsonl_round_trip_reconstructs_conversation(db):
    db.upsert_conversations([make_conv(
        texts=[("user", "あいうえお"), ("assistant", "回答です")],
        msg_ids=["m1", "m2"],
    )])
    records = _dump(db)
    assert len(records) == 1
    rec = records[0]
    # Original fields are reconstructable from the export alone.
    assert rec["source"] == "chatgpt"
    assert rec["source_id"] == "c1"
    assert rec["title"] == "エクスポート対象"
    assert rec["created_at"] == "2025-01-01T00:00:00Z"
    assert rec["updated_at"] == "2025-01-01T00:10:00Z"
    assert [m["text"] for m in rec["messages"]] == ["あいうえお", "回答です"]
    assert [m["role"] for m in rec["messages"]] == ["user", "assistant"]
    assert [m["source_message_id"] for m in rec["messages"]] == ["m1", "m2"]
    # Schema marker + derived placeholder so downstream readers can tell
    # original-from-source data from future Cairn-computed extensions.
    assert rec["schema"] == "cairn.export.v1"
    assert rec["kind"] == "conversation"
    assert rec["derived"] == {}


def test_jsonl_is_one_object_per_line(db):
    db.upsert_conversations([make_conv("chatgpt", "a"), make_conv("claude_cli", "b")])
    buf = io.StringIO()
    db.export_jsonl(buf)
    raw = buf.getvalue()
    lines = raw.splitlines()
    assert len(lines) == 2
    # each line must parse independently as one JSON object
    for ln in lines:
        json.loads(ln)
    # trailing newline so concatenation/append stays clean
    assert raw.endswith("\n")


def test_filter_by_source(db):
    db.upsert_conversations([
        make_conv("chatgpt", "a"),
        make_conv("claude_cli", "b"),
        make_conv("codex_cli", "c"),
    ])
    recs = _dump(db, source="claude_cli")
    assert [r["source_id"] for r in recs] == ["b"]


def test_filter_by_date_range(db):
    db.upsert_conversations([
        make_conv("chatgpt", "old", updated="2024-12-01T00:00:00Z"),
        make_conv("chatgpt", "mid", updated="2025-03-15T00:00:00Z"),
        make_conv("chatgpt", "new", updated="2025-06-01T00:00:00Z"),
    ])
    # after only
    after_recs = _dump(db, after="2025-01-01T00:00:00Z")
    assert sorted(r["source_id"] for r in after_recs) == ["mid", "new"]
    # before only
    before_recs = _dump(db, before="2025-04-01T00:00:00Z")
    assert sorted(r["source_id"] for r in before_recs) == ["mid", "old"]
    # both
    both = _dump(db, after="2025-01-01T00:00:00Z", before="2025-04-01T00:00:00Z")
    assert [r["source_id"] for r in both] == ["mid"]


def test_filter_by_conversation_id(db):
    db.upsert_conversations([make_conv("chatgpt", "a"), make_conv("chatgpt", "b")])
    # rowid 1 is the first inserted
    recs = _dump(db, conversation_id=1)
    assert [r["source_id"] for r in recs] == ["a"]
    # unknown id → empty (not an error)
    assert _dump(db, conversation_id=999) == []


def test_admin_export_jsonl_to_file(db, tmp_path, capsys):
    from app import admin
    importlib.reload(admin)
    db.upsert_conversations([make_conv("chatgpt", "a"), make_conv("claude_cli", "b")])
    out = str(tmp_path / "dump.jsonl")
    rc = admin.main(["export-jsonl", "--out", out, "--source", "claude_cli"])
    assert rc == 0
    # file written, locked down (contains plaintext conversation data)
    assert os.path.exists(out)
    assert oct(os.stat(out).st_mode & 0o777) == "0o600"
    with open(out, encoding="utf-8") as f:
        lines = [ln for ln in f.read().splitlines() if ln]
    assert len(lines) == 1
    assert json.loads(lines[0])["source_id"] == "b"
    # status line lands on stderr so the JSONL can be piped on stdout
    assert "1" in capsys.readouterr().err


def test_admin_export_jsonl_to_stdout(db, capsys):
    from app import admin
    importlib.reload(admin)
    db.upsert_conversations([make_conv("chatgpt", "a")])
    rc = admin.main(["export-jsonl"])
    assert rc == 0
    cap = capsys.readouterr()
    # JSONL on stdout, summary on stderr — keeps pipes clean
    lines = [ln for ln in cap.out.splitlines() if ln]
    assert len(lines) == 1
    assert json.loads(lines[0])["source_id"] == "a"
