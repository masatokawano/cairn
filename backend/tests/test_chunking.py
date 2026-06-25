"""Tests for chunking (P2-1a): the pure algorithm and the DB integration."""
import importlib

import pytest

from app.chunking import (
    CURRENT_CHUNKING_VERSION,
    MAX_CHARS,
    OVERLAP,
    chunk_text,
)


# --- pure algorithm ---------------------------------------------------------

def test_empty_and_whitespace_yield_no_chunks():
    assert chunk_text("") == []
    assert chunk_text("   \n\n  ") == []


def test_short_message_is_one_chunk_spanning_whole_text():
    text = "短いメッセージ"
    chunks = chunk_text(text)
    assert len(chunks) == 1
    c = chunks[0]
    assert (c.idx, c.start_offset, c.end_offset) == (0, 0, len(text))
    assert c.text == text


def test_at_limit_stays_single_chunk():
    text = "a" * MAX_CHARS
    chunks = chunk_text(text)
    assert len(chunks) == 1
    assert chunks[0].end_offset == MAX_CHARS


def test_long_message_splits_with_overlap():
    text = "x" * (MAX_CHARS * 2)  # no paragraph boundaries → fixed-width windows
    chunks = chunk_text(text)
    assert len(chunks) >= 2
    # first window is full width; subsequent windows step by MAX_CHARS - OVERLAP
    assert chunks[0].start_offset == 0
    assert chunks[0].end_offset == MAX_CHARS
    assert chunks[1].start_offset == MAX_CHARS - OVERLAP
    # adjacent chunks overlap by OVERLAP characters
    assert chunks[0].end_offset - chunks[1].start_offset == OVERLAP
    # idx is contiguous from 0
    assert [c.idx for c in chunks] == list(range(len(chunks)))


def test_offsets_round_trip_to_source_text():
    text = "".join(f"段落{i}。" * 80 + "\n\n" for i in range(20))
    chunks = chunk_text(text)
    assert len(chunks) >= 2
    # each chunk's text is exactly the slice it claims
    for c in chunks:
        assert c.text == text[c.start_offset:c.end_offset]
    # chunks cover the whole message with no gaps (overlap means they touch)
    assert chunks[0].start_offset == 0
    assert chunks[-1].end_offset == len(text)
    for prev, nxt in zip(chunks, chunks[1:]):
        assert nxt.start_offset <= prev.end_offset  # contiguous, overlapping


def test_prefers_paragraph_boundary():
    # A blank line in the window's second half should become the split point.
    head = "あ" * (MAX_CHARS - 100)
    text = head + "\n\n" + "い" * MAX_CHARS
    chunks = chunk_text(text)
    # the first chunk ends just after the blank line, not at a hard MAX_CHARS cut
    assert chunks[0].end_offset == len(head) + 2
    assert text[chunks[0].end_offset - 2:chunks[0].end_offset] == "\n\n"


# --- DB integration ---------------------------------------------------------

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


def make_conv(db, source_id="c1", text="本文テキスト"):
    from app.parsers.base import ParsedConversation, ParsedMessage
    return ParsedConversation(
        source="chatgpt", source_id=source_id, title="テスト",
        messages=[ParsedMessage(role="user", text=text, created_at="2025-01-01T00:00:00Z")],
        created_at="2025-01-01T00:00:00Z", updated_at="2025-01-01T00:10:00Z",
    )


def test_upsert_auto_chunks_messages(db):
    db.upsert_conversations([make_conv(db)])
    rows = db.connect().execute(
        "SELECT message_id, text, kind, chunking_version FROM chunks"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["text"] == "本文テキスト"
    assert rows[0]["kind"] == "message_text"
    assert rows[0]["chunking_version"] == CURRENT_CHUNKING_VERSION


def test_update_regenerates_chunks_via_cascade(db):
    db.upsert_conversations([make_conv(db, text="最初の本文")])
    # changing the text forces a re-insert of messages (new ids); old chunks
    # CASCADE-delete and new ones are generated.
    db.upsert_conversations([make_conv(db, text="更新後の本文")])
    rows = db.connect().execute("SELECT text FROM chunks").fetchall()
    assert len(rows) == 1
    assert rows[0]["text"] == "更新後の本文"


def test_rechunk_is_idempotent_then_force_replaces(db):
    db.upsert_conversations([make_conv(db)])
    # already chunked at current version → all skipped
    stats = db.rechunk_messages()
    assert stats == {"messages": 0, "chunks": 0, "skipped": 1}
    # force regenerates (delete + re-insert), no duplication
    stats = db.rechunk_messages(force=True)
    assert stats["messages"] == 1 and stats["skipped"] == 0
    n = db.connect().execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    assert n == 1


def test_rechunk_backfills_after_table_added(db):
    # Simulate a pre-P2-1a DB: message present, its chunks removed.
    db.upsert_conversations([make_conv(db)])
    db.connect().execute("DELETE FROM chunks")
    db.connect().commit()
    stats = db.rechunk_messages()
    assert stats["messages"] == 1 and stats["chunks"] == 1
    assert db.connect().execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 1


def test_integrity_check_reports_chunks_clean(db):
    db.upsert_conversations([make_conv(db)])
    report = db.integrity_check()
    assert report["ok"]
    assert report["checks"]["orphan_chunks"] == 0
