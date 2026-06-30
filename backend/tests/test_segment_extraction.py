"""Tests for P3-C: segment extraction (LLM-based, FixtureProvider)."""
import importlib
import json

import pytest

from app.llm import ValidationError
from app.llm.fixture import FixtureProvider
from app.extraction.segment_runner import (
    SEGMENT_SCHEMA,
    _validate_segment_list,
    run_segment_extraction,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def sdb(tmp_path, monkeypatch):
    monkeypatch.setenv("CAIRN_DB", str(tmp_path / "test.db"))
    from app import db as db_module
    importlib.reload(db_module)
    yield db_module
    db_module._local.__dict__.clear()


def _seed_conversation(sdb, n_messages=4, source_id="c-seg-test") -> tuple[int, list[int]]:
    """Insert a conversation with n_messages and return (conv_id, [msg_id, ...])."""
    from app.parsers.base import ParsedConversation, ParsedMessage
    msgs = [
        ParsedMessage(
            role="user" if i % 2 == 0 else "assistant",
            text=f"Message {i} about topic {'A' if i < n_messages // 2 else 'B'}",
            created_at=f"2025-01-01T00:0{i}:00Z",
        )
        for i in range(n_messages)
    ]
    conv = ParsedConversation(
        source="chatgpt", source_id=source_id, title=f"Test conv {source_id}",
        messages=msgs,
        created_at="2025-01-01T00:00:00Z", updated_at="2025-01-01T00:10:00Z",
    )
    sdb.upsert_conversations([conv])
    conn = sdb.connect()
    conv_id = conn.execute(
        "SELECT id FROM conversations WHERE source_id=?", (source_id,)
    ).fetchone()[0]
    msg_ids = [r[0] for r in conn.execute(
        "SELECT id FROM messages WHERE conversation_id=? ORDER BY id", (conv_id,)
    ).fetchall()]
    return conv_id, msg_ids


# ---------------------------------------------------------------------------
# _validate_segment_list unit tests
# ---------------------------------------------------------------------------

def test_validate_segment_list_ok():
    msg_ids = [1, 2, 3, 4]
    segs = [
        {"start_message_id": 1, "end_message_id": 2, "title": "A", "summary": "s1", "topics": []},
        {"start_message_id": 3, "end_message_id": 4, "title": "B", "summary": "s2", "topics": []},
    ]
    _validate_segment_list(segs, set(msg_ids), msg_ids)  # should not raise


def test_validate_segment_list_unknown_start():
    msg_ids = [1, 2, 3]
    segs = [{"start_message_id": 99, "end_message_id": 2, "title": "X", "summary": "s", "topics": []}]
    with pytest.raises(ValidationError, match="start_message_id 99"):
        _validate_segment_list(segs, set(msg_ids), msg_ids)


def test_validate_segment_list_start_after_end():
    msg_ids = [1, 2, 3]
    segs = [{"start_message_id": 3, "end_message_id": 1, "title": "X", "summary": "s", "topics": []}]
    with pytest.raises(ValidationError, match="comes after end"):
        _validate_segment_list(segs, set(msg_ids), msg_ids)


def test_validate_segment_list_overlap():
    msg_ids = [1, 2, 3, 4]
    segs = [
        {"start_message_id": 1, "end_message_id": 3, "title": "A", "summary": "s", "topics": []},
        {"start_message_id": 2, "end_message_id": 4, "title": "B", "summary": "s", "topics": []},
    ]
    with pytest.raises(ValidationError, match="overlap"):
        _validate_segment_list(segs, set(msg_ids), msg_ids)


def test_validate_segment_list_empty_title():
    msg_ids = [1, 2]
    segs = [{"start_message_id": 1, "end_message_id": 2, "title": "  ", "summary": "s", "topics": []}]
    with pytest.raises(ValidationError, match="title is empty"):
        _validate_segment_list(segs, set(msg_ids), msg_ids)


def test_validate_segment_list_empty_list():
    with pytest.raises(ValidationError, match="empty"):
        _validate_segment_list([], set(), [])


# ---------------------------------------------------------------------------
# run_segment_extraction — FixtureProvider happy path
# ---------------------------------------------------------------------------

def _make_segment_response(msg_ids: list[int]) -> dict:
    """Build a valid segment response covering all msg_ids as a single segment."""
    return {
        "segments": [{
            "start_message_id": msg_ids[0],
            "end_message_id": msg_ids[-1],
            "title": "Test segment",
            "summary": "A test summary.",
            "topics": ["testing", "example"],
        }]
    }


def _make_two_segment_response(msg_ids: list[int]) -> dict:
    mid = len(msg_ids) // 2
    return {
        "segments": [
            {
                "start_message_id": msg_ids[0],
                "end_message_id": msg_ids[mid - 1],
                "title": "First half",
                "summary": "Summary of first half.",
                "topics": ["topic-a"],
            },
            {
                "start_message_id": msg_ids[mid],
                "end_message_id": msg_ids[-1],
                "title": "Second half",
                "summary": "Summary of second half.",
                "topics": ["topic-b"],
            },
        ]
    }


def test_run_segment_extraction_single_segment(sdb):
    conv_id, msg_ids = _seed_conversation(sdb, n_messages=4)
    provider = FixtureProvider(responses=[_make_segment_response(msg_ids)])

    summary = run_segment_extraction(provider, conversation_id=conv_id)

    assert summary["conversations"] == 1
    assert summary["segments"] == 1
    assert summary["warnings"] == 0
    segs = sdb.list_segments(conversation_id=conv_id)
    assert len(segs) == 1
    assert segs[0]["title"] == "Test segment"
    assert segs[0]["start_message_id"] == msg_ids[0]
    assert segs[0]["end_message_id"] == msg_ids[-1]
    assert segs[0]["locked_by_user"] == 0


def test_run_segment_extraction_two_segments(sdb):
    conv_id, msg_ids = _seed_conversation(sdb, n_messages=6)
    provider = FixtureProvider(responses=[_make_two_segment_response(msg_ids)])

    summary = run_segment_extraction(provider, conversation_id=conv_id)

    assert summary["segments"] == 2
    segs = sdb.list_segments(conversation_id=conv_id)
    assert len(segs) == 2
    assert segs[0]["idx"] == 0
    assert segs[1]["idx"] == 1


def test_run_segment_extraction_skips_existing(sdb):
    """Without force=True, already-segmented conversations are skipped."""
    conv_id, msg_ids = _seed_conversation(sdb, n_messages=4)
    provider = FixtureProvider(responses=[
        _make_segment_response(msg_ids),
        _make_segment_response(msg_ids),
    ])

    run_segment_extraction(provider, conversation_id=conv_id)
    run_segment_extraction(provider, conversation_id=conv_id)  # should skip

    assert len(sdb.list_segments(conversation_id=conv_id)) == 1
    assert provider.calls == 1  # only called once


def test_run_segment_extraction_force_regenerates(sdb):
    """force=True deletes unlocked segments and regenerates."""
    conv_id, msg_ids = _seed_conversation(sdb, n_messages=4)
    provider = FixtureProvider(responses=[
        _make_segment_response(msg_ids),
        _make_two_segment_response(msg_ids),
    ])

    run_segment_extraction(provider, conversation_id=conv_id)
    assert len(sdb.list_segments(conversation_id=conv_id)) == 1

    run_segment_extraction(provider, conversation_id=conv_id, force=True)
    # Second run used two-segment response — should now have 2 segments.
    segs = sdb.list_segments(conversation_id=conv_id)
    assert len(segs) == 2


def test_run_segment_extraction_force_preserves_locked(sdb):
    """force=True must not delete locked_by_user=1 segments."""
    conv_id, msg_ids = _seed_conversation(sdb, n_messages=4)
    provider = FixtureProvider(responses=[_make_segment_response(msg_ids)])

    run_segment_extraction(provider, conversation_id=conv_id)
    # Lock the segment.
    sdb.connect().execute(
        "UPDATE segments SET locked_by_user=1 WHERE conversation_id=?", (conv_id,)
    )
    sdb.connect().commit()

    provider2 = FixtureProvider(responses=[_make_segment_response(msg_ids)])
    run_segment_extraction(provider2, conversation_id=conv_id, force=True)

    segs = sdb.list_segments(conversation_id=conv_id)
    locked = [s for s in segs if s["locked_by_user"] == 1]
    assert len(locked) == 1


def test_run_segment_extraction_retry_on_bad_grounding(sdb):
    """If LLM returns an invalid message_id, validate retries and succeeds."""
    conv_id, msg_ids = _seed_conversation(sdb, n_messages=4)
    bad_response = {
        "segments": [{
            "start_message_id": 99999,  # invalid
            "end_message_id": msg_ids[-1],
            "title": "Bad",
            "summary": "s",
            "topics": [],
        }]
    }
    good_response = _make_segment_response(msg_ids)
    provider = FixtureProvider(responses=[bad_response, good_response])

    summary = run_segment_extraction(provider, conversation_id=conv_id, max_retries=3)

    assert summary["segments"] == 1
    assert summary["warnings"] == 0
    segs = sdb.list_segments(conversation_id=conv_id)
    assert segs[0]["start_message_id"] == msg_ids[0]


def test_run_segment_extraction_exhausted_retries_records_warning(sdb):
    """When retries are exhausted the conversation is skipped and warning recorded."""
    conv_id, msg_ids = _seed_conversation(sdb, n_messages=4)
    bad_response = {
        "segments": [{
            "start_message_id": 99999,
            "end_message_id": 99999,
            "title": "X",
            "summary": "s",
            "topics": [],
        }]
    }
    provider = FixtureProvider(responses=[bad_response, bad_response, bad_response, bad_response])

    summary = run_segment_extraction(provider, conversation_id=conv_id, max_retries=2)

    assert summary["segments"] == 0
    assert summary["warnings"] == 1
    assert len(sdb.list_segments(conversation_id=conv_id)) == 0


def test_run_segment_extraction_records_extraction_run(sdb):
    conv_id, msg_ids = _seed_conversation(sdb, n_messages=4)
    provider = FixtureProvider(responses=[_make_segment_response(msg_ids)])

    summary = run_segment_extraction(provider, conversation_id=conv_id)

    runs = sdb.list_extraction_runs(kind="segment")
    assert len(runs) >= 1
    assert runs[0]["id"] == summary["run_id"]
    assert runs[0]["status"] == "ok"
    assert runs[0]["provider"] == "fixture"


def test_run_segment_extraction_limit(sdb):
    """limit=1 processes at most 1 conversation."""
    _seed_conversation(sdb, n_messages=4, source_id="c1")
    _seed_conversation(sdb, n_messages=4, source_id="c2")

    conv_ids = [r[0] for r in sdb.connect().execute("SELECT id FROM conversations ORDER BY id")]
    msg_ids_c1 = [r[0] for r in sdb.connect().execute(
        "SELECT id FROM messages WHERE conversation_id=? ORDER BY id", (conv_ids[0],)
    ).fetchall()]
    provider = FixtureProvider(responses=[_make_segment_response(msg_ids_c1)])
    summary = run_segment_extraction(provider, limit=1)
    assert summary["conversations"] == 1
