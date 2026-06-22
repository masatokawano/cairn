"""Idempotency regression tests + per-message stable id storage (P1-C).

Guards the diff-import contract: re-importing identical input never
duplicates, an edited conversation updates in place, and source_message_id
is persisted and returned.
"""
import importlib

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


def make_conv(db, source_id="c1", texts=None, msg_ids=None):
    from app.parsers.base import ParsedConversation, ParsedMessage
    texts = texts or [("user", "最初の質問"), ("assistant", "最初の回答")]
    msg_ids = msg_ids or [None] * len(texts)
    return ParsedConversation(
        source="chatgpt", source_id=source_id, title="冪等性テスト",
        messages=[
            ParsedMessage(role=r, text=t, created_at="2025-01-01T00:00:00Z", source_message_id=mid)
            for (r, t), mid in zip(texts, msg_ids)
        ],
        created_at="2025-01-01T00:00:00Z", updated_at="2025-01-01T00:10:00Z",
    )


def _counts(db):
    conn = db.connect()
    convs = conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
    msgs = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    return convs, msgs


def test_repeated_import_of_same_input_never_duplicates(db):
    conv = make_conv(db)
    assert db.upsert_conversations([conv]) == {"inserted": 1, "updated": 0, "skipped": 0}
    # import the same content several more times
    for _ in range(3):
        assert db.upsert_conversations([conv]) == {"inserted": 0, "updated": 0, "skipped": 1}
    assert _counts(db) == (1, 2)  # no duplicated rows


def test_edited_conversation_updates_in_place(db):
    db.upsert_conversations([make_conv(db)])
    # edit: change a message + add one
    edited = make_conv(db, texts=[("user", "最初の質問"), ("assistant", "修正した回答"), ("user", "追加")])
    assert db.upsert_conversations([edited]) == {"inserted": 0, "updated": 1, "skipped": 0}
    convs, msgs = _counts(db)
    assert convs == 1            # still one conversation (no duplicate)
    assert msgs == 3            # messages fully replaced, not appended
    full = db.get_conversation(1)
    assert [m["text"] for m in full["messages"]] == ["最初の質問", "修正した回答", "追加"]
    # re-importing the edited version is now a skip
    assert db.upsert_conversations([edited]) == {"inserted": 0, "updated": 0, "skipped": 1}


def test_mixed_batch_inserts_updates_and_skips(db):
    db.upsert_conversations([make_conv(db, "a"), make_conv(db, "b")])
    batch = [
        make_conv(db, "a"),                                   # unchanged → skip
        make_conv(db, "b", texts=[("user", "x"), ("assistant", "y")]),  # changed → update
        make_conv(db, "c"),                                   # new → insert
    ]
    assert db.upsert_conversations(batch) == {"inserted": 1, "updated": 1, "skipped": 1}
    assert _counts(db)[0] == 3


def test_source_message_id_persisted_and_returned(db):
    conv = make_conv(db, texts=[("user", "q"), ("assistant", "a")], msg_ids=["msg-1", "msg-2"])
    db.upsert_conversations([conv])
    full = db.get_conversation(1)
    assert [m["source_message_id"] for m in full["messages"]] == ["msg-1", "msg-2"]
    # missing source ids stay None (codex/gemini case)
    conv2 = make_conv(db, "c2")
    db.upsert_conversations([conv2])
    assert all(m["source_message_id"] is None for m in db.get_conversation(2)["messages"])


def test_migration_adds_source_message_id_to_pre_v3_db(db):
    # build the DB, then simulate a pre-v3 shape: drop the column by rebuilding
    # messages without it and roll user_version back to 2.
    db.upsert_conversations([make_conv(db)])
    conn = db.connect()
    with conn:
        conn.execute("ALTER TABLE messages DROP COLUMN source_message_id")
        conn.execute("PRAGMA user_version = 2")
    conn.close()
    db._local.conn = None

    conn = db.connect()  # reopen → migration v3 runs
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
    cols = [r[1] for r in conn.execute("PRAGMA table_info(messages)")]
    assert "source_message_id" in cols
    # existing data still intact
    assert db.get_conversation(1) is not None
