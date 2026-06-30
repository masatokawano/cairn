"""Tests for P3-D: assertion extraction (LLM-based, FixtureProvider)."""
import importlib
import json

import pytest

from app.llm import ValidationError
from app.llm.fixture import FixtureProvider
from app.extraction.assertion_runner import (
    VALID_ACTORS, VALID_KINDS, VALID_STATUSES,
    _validate_enum_fields,
    run_assertion_extraction,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@pytest.fixture()
def adb(tmp_path, monkeypatch):
    monkeypatch.setenv("CAIRN_DB", str(tmp_path / "test.db"))
    from app import db as db_module
    importlib.reload(db_module)
    yield db_module
    db_module._local.__dict__.clear()


def _seed(adb, n_messages=4, source_id="c-assert") -> tuple[int, int, list[int]]:
    """Seed conv + messages + one segment. Returns (conv_id, seg_id, msg_ids)."""
    from app.parsers.base import ParsedConversation, ParsedMessage
    msgs = [
        ParsedMessage(
            role="user" if i % 2 == 0 else "assistant",
            text=f"Message {i}",
            created_at=f"2025-01-01T00:0{i}:00Z",
        )
        for i in range(n_messages)
    ]
    conv = ParsedConversation(
        source="chatgpt", source_id=source_id, title="test",
        messages=msgs,
        created_at="2025-01-01T00:00:00Z", updated_at="2025-01-01T00:10:00Z",
    )
    adb.upsert_conversations([conv])
    conn = adb.connect()
    conv_id = conn.execute(
        "SELECT id FROM conversations WHERE source_id=?", (source_id,)
    ).fetchone()[0]
    msg_ids = [r[0] for r in conn.execute(
        "SELECT id FROM messages WHERE conversation_id=? ORDER BY id", (conv_id,)
    ).fetchall()]
    seg_id = adb.insert_segment(
        conversation_id=conv_id, idx=0,
        start_message_id=msg_ids[0], end_message_id=msg_ids[-1],
        title="Test segment", summary="A test summary.",
        generated_by="fixture:fixture-v1", prompt_version="segment-v1",
        created_at="2026-01-01T00:00:00",
    )
    return conv_id, seg_id, msg_ids


def _make_assertion(msg_ids: list[int], **kwargs) -> dict:
    base = {
        "text": "The system uses SQLite for storage.",
        "actor": "assistant",
        "kind": "claim",
        "status": "tentative",
        "confidence": 0.9,
        "supporting_message_ids": [msg_ids[0]] if msg_ids else [],
    }
    base.update(kwargs)
    return base


def _make_response(msg_ids: list[int], assertions: list[dict] | None = None) -> dict:
    if assertions is None:
        assertions = [_make_assertion(msg_ids)]
    return {"assertions": assertions}


# ---------------------------------------------------------------------------
# _validate_enum_fields unit tests
# ---------------------------------------------------------------------------

def test_validate_enum_fields_ok():
    items = [_make_assertion([1])]
    assert _validate_enum_fields(items) is None


def test_validate_enum_fields_bad_actor():
    items = [_make_assertion([1], actor="robot")]
    err = _validate_enum_fields(items)
    assert err is not None
    assert "actor" in err


def test_validate_enum_fields_bad_kind():
    items = [_make_assertion([1], kind="complaint")]
    err = _validate_enum_fields(items)
    assert err is not None
    assert "kind" in err


def test_validate_enum_fields_bad_status():
    items = [_make_assertion([1], status="maybe")]
    err = _validate_enum_fields(items)
    assert err is not None
    assert "status" in err


def test_validate_enum_fields_confidence_out_of_range():
    items = [_make_assertion([1], confidence=1.5)]
    err = _validate_enum_fields(items)
    assert err is not None
    assert "confidence" in err


def test_validate_enum_fields_empty_text():
    items = [_make_assertion([1], text="  ")]
    err = _validate_enum_fields(items)
    assert err is not None
    assert "text" in err


def test_all_valid_actor_values():
    for actor in VALID_ACTORS:
        assert _validate_enum_fields([_make_assertion([1], actor=actor)]) is None


def test_all_valid_kind_values():
    for kind in VALID_KINDS:
        assert _validate_enum_fields([_make_assertion([1], kind=kind)]) is None


def test_all_valid_status_values():
    for status in VALID_STATUSES:
        assert _validate_enum_fields([_make_assertion([1], status=status)]) is None


# ---------------------------------------------------------------------------
# run_assertion_extraction — happy path
# ---------------------------------------------------------------------------

def test_run_basic(adb):
    conv_id, seg_id, msg_ids = _seed(adb)
    provider = FixtureProvider(responses=[_make_response(msg_ids)])

    summary = run_assertion_extraction(provider, segment_id=seg_id)

    assert summary["segments"] == 1
    assert summary["assertions"] == 1
    assert summary["warnings"] == 0
    rows = adb.list_assertions(segment_id=seg_id)
    assert len(rows) == 1
    assert rows[0]["actor"] == "assistant"
    assert rows[0]["kind"] == "claim"
    assert rows[0]["locked_by_user"] == 0


def test_run_multiple_assertions(adb):
    conv_id, seg_id, msg_ids = _seed(adb)
    assertions = [
        _make_assertion(msg_ids, kind="claim"),
        _make_assertion(msg_ids, kind="decision", actor="user",
                        text="We will use FastAPI."),
        _make_assertion(msg_ids, kind="question", actor="user",
                        status="unresolved", text="Which DB to use?"),
    ]
    provider = FixtureProvider(responses=[_make_response(msg_ids, assertions)])

    summary = run_assertion_extraction(provider, segment_id=seg_id)

    assert summary["assertions"] == 3
    rows = adb.list_assertions(segment_id=seg_id)
    kinds = {r["kind"] for r in rows}
    assert kinds == {"claim", "decision", "question"}


def test_run_skips_existing_without_force(adb):
    conv_id, seg_id, msg_ids = _seed(adb)
    provider = FixtureProvider(responses=[
        _make_response(msg_ids),
        _make_response(msg_ids),
    ])
    run_assertion_extraction(provider, segment_id=seg_id)
    run_assertion_extraction(provider, segment_id=seg_id)  # should skip

    assert len(adb.list_assertions(segment_id=seg_id)) == 1
    assert provider.calls == 1


def test_run_force_regenerates(adb):
    conv_id, seg_id, msg_ids = _seed(adb)
    two_assertions = [_make_assertion(msg_ids), _make_assertion(msg_ids, kind="decision", text="Use Redis.")]
    provider = FixtureProvider(responses=[
        _make_response(msg_ids),
        _make_response(msg_ids, two_assertions),
    ])
    run_assertion_extraction(provider, segment_id=seg_id)
    assert len(adb.list_assertions(segment_id=seg_id)) == 1

    run_assertion_extraction(provider, segment_id=seg_id, force=True)
    assert len(adb.list_assertions(segment_id=seg_id)) == 2


def test_run_force_preserves_locked(adb):
    conv_id, seg_id, msg_ids = _seed(adb)
    provider = FixtureProvider(responses=[_make_response(msg_ids)])
    run_assertion_extraction(provider, segment_id=seg_id)
    adb.connect().execute(
        "UPDATE assertions SET locked_by_user=1 WHERE segment_id=?", (seg_id,)
    )
    adb.connect().commit()

    provider2 = FixtureProvider(responses=[_make_response(msg_ids)])
    run_assertion_extraction(provider2, segment_id=seg_id, force=True)

    rows = adb.list_assertions(segment_id=seg_id)
    locked = [r for r in rows if r["locked_by_user"] == 1]
    assert len(locked) == 1


def test_run_grounding_retry(adb):
    """Invalid supporting_message_id triggers retry."""
    conv_id, seg_id, msg_ids = _seed(adb)
    bad = _make_response(msg_ids, [_make_assertion(msg_ids, supporting_message_ids=[99999])])
    good = _make_response(msg_ids)
    provider = FixtureProvider(responses=[bad, good])

    summary = run_assertion_extraction(provider, segment_id=seg_id, max_retries=3)

    assert summary["assertions"] == 1
    assert summary["warnings"] == 0
    row = adb.list_assertions(segment_id=seg_id)[0]
    assert 99999 not in json.loads(row["supporting_message_ids"])


def test_run_exhausted_retries_records_warning(adb):
    conv_id, seg_id, msg_ids = _seed(adb)
    bad = _make_response(msg_ids, [_make_assertion(msg_ids, supporting_message_ids=[99999])])
    provider = FixtureProvider(responses=[bad, bad, bad, bad])

    summary = run_assertion_extraction(provider, segment_id=seg_id, max_retries=2)

    assert summary["assertions"] == 0
    assert summary["warnings"] == 1
    assert len(adb.list_assertions(segment_id=seg_id)) == 0


def test_run_enum_retry(adb):
    """Bad enum in first response triggers retry."""
    conv_id, seg_id, msg_ids = _seed(adb)
    bad = _make_response(msg_ids, [_make_assertion(msg_ids, actor="robot")])
    good = _make_response(msg_ids)
    provider = FixtureProvider(responses=[bad, good])

    summary = run_assertion_extraction(provider, segment_id=seg_id, max_retries=2)

    assert summary["assertions"] == 1
    assert summary["warnings"] == 0


def test_run_records_extraction_run(adb):
    conv_id, seg_id, msg_ids = _seed(adb)
    provider = FixtureProvider(responses=[_make_response(msg_ids)])

    summary = run_assertion_extraction(provider, segment_id=seg_id)

    runs = adb.list_extraction_runs(kind="assertion")
    assert len(runs) >= 1
    assert runs[0]["id"] == summary["run_id"]
    assert runs[0]["status"] == "ok"
    assert runs[0]["provider"] == "fixture"


def test_run_limit(adb):
    """limit=1 processes at most 1 segment."""
    conv_id, seg_id1, msg_ids1 = _seed(adb, source_id="c1")
    _, seg_id2, msg_ids2 = _seed(adb, source_id="c2")

    provider = FixtureProvider(responses=[_make_response(msg_ids1)])
    summary = run_assertion_extraction(provider, limit=1)
    assert summary["segments"] == 1


def test_supporting_message_ids_stored_as_json(adb):
    conv_id, seg_id, msg_ids = _seed(adb, n_messages=4)
    ids_to_use = msg_ids[:2]
    provider = FixtureProvider(responses=[
        _make_response(msg_ids, [_make_assertion(msg_ids, supporting_message_ids=ids_to_use)])
    ])
    run_assertion_extraction(provider, segment_id=seg_id)

    row = adb.list_assertions(segment_id=seg_id)[0]
    stored = json.loads(row["supporting_message_ids"])
    assert stored == ids_to_use
