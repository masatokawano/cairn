"""Attachment metadata tests (P1-H).

Receipt criteria from docs/architecture-audit.md:
- attachments are tied to conversation/message
- a missing attachment doesn't break conversation ingest (additive)
- derived-text storage (e.g. future OCR/PDF text) lives in its own column
"""
import base64
import hashlib
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


def _make_attachment(media_type="application/pdf", data=b"%PDF-1.0 stub"):
    from app.parsers.base import ParsedAttachment
    return ParsedAttachment(
        source_ref=None,
        mime=media_type,
        size=len(data),
        hash=hashlib.sha256(data).hexdigest(),
        extracted_text=None,
    )


def _make_conv(source_id="c1", with_attachment=False, attachments_per_message=None):
    from app.parsers.base import ParsedConversation, ParsedMessage
    if attachments_per_message is None:
        msgs = [
            ParsedMessage(role="user", text="質問",
                          created_at="2025-01-01T00:00:00Z",
                          attachments=[_make_attachment()] if with_attachment else []),
            ParsedMessage(role="assistant", text="回答",
                          created_at="2025-01-01T00:01:00Z"),
        ]
    else:
        msgs = [
            ParsedMessage(role="user", text="質問",
                          created_at="2025-01-01T00:00:00Z",
                          attachments=attachments_per_message),
        ]
    return ParsedConversation(
        source="claude_cli", source_id=source_id, title="添付テスト",
        messages=msgs,
        created_at="2025-01-01T00:00:00Z", updated_at="2025-01-01T00:10:00Z",
    )


def test_attachments_table_created_on_fresh_db(db):
    conn = db.connect()
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "attachments" in tables
    # FK cascade in place so deleting a conversation removes its attachments.
    fk = list(conn.execute("PRAGMA foreign_key_list(attachments)"))
    assert any(r["table"] == "conversations" and r["on_delete"] == "CASCADE" for r in fk)


def test_migration_v4_adds_attachments_to_pre_v4_db(db):
    # Build the DB, then simulate a pre-v4 shape: drop the table and roll back.
    # A pre-v4 shape has none of the v11 items registry either — drop those
    # artefacts too so migration 11's non-idempotent ALTER TABLE does not
    # collide with the pre-existing item_id column on the fresh build.
    db.upsert_conversations([_make_conv()])
    conn = db.connect()
    with conn:
        conn.execute("DROP INDEX IF EXISTS idx_chunks_item")
        conn.execute("ALTER TABLE chunks DROP COLUMN item_id")
        conn.execute("DROP TABLE IF EXISTS item_links")
        conn.execute("DROP TABLE IF EXISTS items")
        conn.execute("DROP TABLE IF EXISTS sync_state")
        conn.execute("DROP TABLE attachments")
        conn.execute("PRAGMA user_version = 3")
    conn.close()
    db._local.conn = None

    conn = db.connect()  # reopen → migration v4 (and any later) runs
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db._SCHEMA_VERSION
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "attachments" in tables
    # Existing data still intact.
    assert db.get_conversation(1) is not None


def test_ingest_persists_attachment_metadata(db):
    db.upsert_conversations([_make_conv(with_attachment=True)])
    conv = db.get_conversation(1)
    # Attachments are attached to their message, not the conversation as a whole.
    user_msg = conv["messages"][0]
    assert len(user_msg["attachments"]) == 1
    att = user_msg["attachments"][0]
    assert att["mime"] == "application/pdf"
    assert att["size"] == len(b"%PDF-1.0 stub")
    assert att["hash"] == hashlib.sha256(b"%PDF-1.0 stub").hexdigest()
    # extracted_text is reserved for future OCR/PDF passes — None until then.
    assert att["extracted_text"] is None
    # Assistant message has no attachments → empty list, not missing.
    assert conv["messages"][1]["attachments"] == []


def test_ingest_without_attachments_unchanged_hash(db):
    # Inserting a conv with no attachments must produce the SAME content_hash
    # as before P1-H — otherwise every pre-existing conversation would update
    # on next sync. Anchor against a value computed without the attachment
    # branch to guard against accidental hash drift.
    from app.parsers.base import ParsedConversation, ParsedMessage
    plain = ParsedConversation(
        source="claude_cli", source_id="c1", title="t",
        messages=[ParsedMessage(role="user", text="q", created_at="2025")],
    )
    import hashlib as h, json
    expected = h.sha256(
        json.dumps([("user", "q", "2025")], ensure_ascii=False).encode()
    ).hexdigest()
    assert plain.content_hash() == expected


def test_attachment_change_triggers_update(db):
    db.upsert_conversations([_make_conv(with_attachment=True)])
    # Re-import unchanged → skip.
    assert db.upsert_conversations([_make_conv(with_attachment=True)]) == \
        {"inserted": 0, "updated": 0, "skipped": 1}
    # Different attachment bytes → different hash → conversation updates.
    diff = _make_conv(attachments_per_message=[
        _make_attachment(data=b"%PDF-1.0 different bytes")
    ])
    assert db.upsert_conversations([diff]) == \
        {"inserted": 0, "updated": 1, "skipped": 0}
    conv = db.get_conversation(1)
    assert conv["messages"][0]["attachments"][0]["hash"] == \
        hashlib.sha256(b"%PDF-1.0 different bytes").hexdigest()


def test_attachment_cascade_on_conversation_delete(db):
    db.upsert_conversations([_make_conv(with_attachment=True)])
    conn = db.connect()
    assert conn.execute("SELECT COUNT(*) FROM attachments").fetchone()[0] == 1
    with conn:
        conn.execute("DELETE FROM conversations WHERE id=1")
    assert conn.execute("SELECT COUNT(*) FROM attachments").fetchone()[0] == 0


def test_attachment_cascade_when_messages_replaced_on_update(db):
    # When a conversation's content_hash changes, upsert_conversations
    # deletes-and-reinserts its messages. Attachments hang off message_id
    # via FK CASCADE so they must NOT survive the rewrite — otherwise stale
    # attachments would orphan to messages that no longer exist.
    db.upsert_conversations([_make_conv(with_attachment=True)])
    edited = _make_conv(attachments_per_message=[_make_attachment(data=b"new")])
    db.upsert_conversations([edited])
    conn = db.connect()
    # Exactly one attachment (the new one), zero orphans.
    assert conn.execute("SELECT COUNT(*) FROM attachments").fetchone()[0] == 1
    integrity = db.integrity_check()
    assert integrity["ok"] is True
    # The table now exists, so the orphan-attachments check is active.
    assert "orphan_attachments" in integrity["checks"]


def test_claude_cli_extracts_document_block_as_attachment():
    # Real-shape claude_cli line carrying a PDF as a base64-encoded document
    # block alongside the user's text. Parser must keep the text AND surface
    # the document as a ParsedAttachment with mime / size / hash from the
    # decoded bytes — without storing the bytes themselves.
    import json as j
    from app.parsers import claude_cli
    pdf_bytes = b"%PDF-1.0 minimal"
    b64 = base64.b64encode(pdf_bytes).decode()
    line = j.dumps({
        "type": "user",
        "uuid": "msg-1",
        "sessionId": "s1",
        "cwd": "/tmp",
        "timestamp": "2025-01-01T00:00:00Z",
        "message": {
            "content": [
                {"type": "text", "text": "見て"},
                {"type": "document", "source": {
                    "type": "base64", "media_type": "application/pdf", "data": b64,
                }},
            ],
        },
    })
    result = claude_cli.parse_file("/tmp/s.jsonl", line)
    assert len(result.conversations) == 1
    msgs = result.conversations[0].messages
    assert len(msgs) == 1
    assert msgs[0].text == "見て"
    assert len(msgs[0].attachments) == 1
    att = msgs[0].attachments[0]
    assert att.mime == "application/pdf"
    assert att.size == len(pdf_bytes)
    assert att.hash == hashlib.sha256(pdf_bytes).hexdigest()
    # source_ref is None for inline-embedded attachments (no path to point at).
    assert att.source_ref is None
