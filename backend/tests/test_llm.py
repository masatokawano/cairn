"""Tests for Phase 3 P3-A: LLMProvider abstraction + extraction validation layer."""
import importlib

import pytest

from app.llm import ValidationError
from app.llm.fixture import FixtureProvider, _derive_from_schema
from app.extraction.validate import (
    GroundingContext,
    extract_with_validation,
)

# ---------------------------------------------------------------------------
# FixtureProvider basic behaviour
# ---------------------------------------------------------------------------

SIMPLE_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "topics": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": "string"},
    },
    "required": ["title", "topics", "summary"],
}


def test_fixture_provider_name_model():
    p = FixtureProvider()
    assert p.name == "fixture"
    assert p.model == "fixture-v1"


def test_fixture_provider_returns_required_fields():
    p = FixtureProvider()
    result = p.complete_structured("test prompt", schema=SIMPLE_SCHEMA)
    assert "title" in result
    assert "topics" in result
    assert "summary" in result
    assert isinstance(result["title"], str)
    assert isinstance(result["topics"], list)


def test_fixture_provider_deterministic():
    p1 = FixtureProvider()
    p2 = FixtureProvider()
    r1 = p1.complete_structured("same prompt", schema=SIMPLE_SCHEMA)
    r2 = p2.complete_structured("same prompt", schema=SIMPLE_SCHEMA)
    assert r1 == r2


def test_fixture_provider_different_prompts_differ():
    p = FixtureProvider()
    r1 = p.complete_structured("prompt A", schema=SIMPLE_SCHEMA)
    r2 = p.complete_structured("prompt B", schema=SIMPLE_SCHEMA)
    assert r1 != r2


def test_fixture_provider_enum_field():
    schema = {
        "type": "object",
        "properties": {"actor": {"type": "string", "enum": ["user", "assistant", "shared"]}},
        "required": ["actor"],
    }
    p = FixtureProvider()
    result = p.complete_structured("test", schema=schema)
    assert result["actor"] in ("user", "assistant", "shared")


def test_fixture_provider_fail_first():
    p = FixtureProvider(fail_first=2)
    with pytest.raises(ValidationError):
        p.complete_structured("prompt", schema=SIMPLE_SCHEMA)
    with pytest.raises(ValidationError):
        p.complete_structured("prompt", schema=SIMPLE_SCHEMA)
    # Third call succeeds
    result = p.complete_structured("prompt", schema=SIMPLE_SCHEMA)
    assert "title" in result
    assert p.calls == 3


def test_fixture_provider_explicit_responses():
    p = FixtureProvider(responses=[{"title": "A", "topics": [], "summary": "s"}])
    result = p.complete_structured("x", schema=SIMPLE_SCHEMA)
    assert result["title"] == "A"
    # After queue exhausted, falls back to hash-derived
    result2 = p.complete_structured("x", schema=SIMPLE_SCHEMA)
    assert "title" in result2


def test_fixture_estimate_tokens():
    p = FixtureProvider()
    assert p.estimate_tokens("hello world") > 0
    assert p.estimate_tokens("") == 1  # max(1, ...)


# ---------------------------------------------------------------------------
# extract_with_validation — happy path
# ---------------------------------------------------------------------------

def test_extract_success_no_retry():
    p = FixtureProvider()
    result = extract_with_validation(p, "summarize this", schema=SIMPLE_SCHEMA)
    assert result.retries == 0
    assert "title" in result.data
    assert result.input_tokens > 0


def test_extract_retry_on_provider_failure():
    p = FixtureProvider(fail_first=1)
    result = extract_with_validation(p, "summarize this", schema=SIMPLE_SCHEMA, max_retries=3)
    assert result.retries == 1
    assert "title" in result.data


def test_extract_exhausts_retries():
    p = FixtureProvider(fail_first=99)
    with pytest.raises(ValidationError):
        extract_with_validation(p, "summarize", schema=SIMPLE_SCHEMA, max_retries=2)
    assert p.calls == 3  # initial + 2 retries


# ---------------------------------------------------------------------------
# extract_with_validation — schema validation
# ---------------------------------------------------------------------------

def test_extract_schema_missing_required():
    p = FixtureProvider(responses=[{"title": "T", "topics": []}])  # missing summary
    with pytest.raises(ValidationError, match="summary"):
        extract_with_validation(p, "prompt", schema=SIMPLE_SCHEMA, max_retries=0)


def test_extract_schema_wrong_type():
    p = FixtureProvider(responses=[{"title": 42, "topics": [], "summary": "s"}])
    with pytest.raises(ValidationError):
        extract_with_validation(p, "prompt", schema=SIMPLE_SCHEMA, max_retries=0)


def test_extract_schema_bad_enum():
    schema = {
        "type": "object",
        "properties": {"actor": {"type": "string", "enum": ["user", "assistant"]}},
        "required": ["actor"],
    }
    p = FixtureProvider(responses=[{"actor": "robot"}])
    with pytest.raises(ValidationError):
        extract_with_validation(p, "prompt", schema=schema, max_retries=0)


def test_extract_schema_retry_succeeds_after_bad_response():
    p = FixtureProvider(responses=[
        {"title": "T", "topics": []},                   # missing summary → schema error
        {"title": "T", "topics": [], "summary": "ok"},  # valid
    ])
    result = extract_with_validation(p, "prompt", schema=SIMPLE_SCHEMA, max_retries=2)
    assert result.retries == 1
    assert result.data["summary"] == "ok"


# ---------------------------------------------------------------------------
# extract_with_validation — grounding validation
# ---------------------------------------------------------------------------

GROUNDING_SCHEMA = {
    "type": "object",
    "properties": {
        "text": {"type": "string"},
        "supporting_message_ids": {"type": "array", "items": {"type": "integer"}},
    },
    "required": ["text", "supporting_message_ids"],
}


def test_extract_grounding_valid_ids():
    p = FixtureProvider(responses=[{"text": "claim", "supporting_message_ids": [1, 2]}])
    ctx = GroundingContext(valid_message_ids={1, 2, 3})
    result = extract_with_validation(p, "extract", schema=GROUNDING_SCHEMA, grounding=ctx, max_retries=0)
    assert result.data["supporting_message_ids"] == [1, 2]


def test_extract_grounding_invalid_id_causes_retry():
    p = FixtureProvider(responses=[
        {"text": "claim", "supporting_message_ids": [99]},  # 99 not in context
        {"text": "claim", "supporting_message_ids": [1]},   # valid
    ])
    ctx = GroundingContext(valid_message_ids={1, 2})
    result = extract_with_validation(p, "extract", schema=GROUNDING_SCHEMA, grounding=ctx, max_retries=2)
    assert result.retries == 1
    assert result.data["supporting_message_ids"] == [1]


def test_extract_grounding_exhausted():
    p = FixtureProvider(responses=[
        {"text": "x", "supporting_message_ids": [99]},
        {"text": "x", "supporting_message_ids": [99]},
        {"text": "x", "supporting_message_ids": [99]},
    ])
    ctx = GroundingContext(valid_message_ids={1})
    with pytest.raises(ValidationError, match="grounding failed"):
        extract_with_validation(p, "extract", schema=GROUNDING_SCHEMA, grounding=ctx, max_retries=2)


def test_extract_no_grounding_context():
    """supporting_message_ids present but no GroundingContext → skip grounding check."""
    schema = {
        "type": "object",
        "properties": {
            "text": {"type": "string"},
            "supporting_message_ids": {"type": "array", "items": {"type": "integer"}},
        },
        "required": ["text"],
    }
    p = FixtureProvider(responses=[{"text": "hi", "supporting_message_ids": [9999]}])
    result = extract_with_validation(p, "x", schema=schema, grounding=None, max_retries=0)
    assert result.data["supporting_message_ids"] == [9999]


# ---------------------------------------------------------------------------
# DB: extraction_runs CRUD
# ---------------------------------------------------------------------------

@pytest.fixture()
def extdb(tmp_path, monkeypatch):
    monkeypatch.setenv("CAIRN_DB", str(tmp_path / "test.db"))
    from app import db as db_module
    importlib.reload(db_module)
    yield db_module
    db_module._local.__dict__.clear()


def test_extraction_run_start_finish(extdb):
    run_id = extdb.start_extraction_run(
        kind="segment",
        scope="all",
        provider="fixture",
        model="fixture-v1",
        prompt_version="segment-v1",
        started_at="2026-06-30T00:00:00",
    )
    assert run_id > 0
    runs = extdb.list_extraction_runs()
    assert runs[0]["status"] == "running"

    extdb.finish_extraction_run(
        run_id,
        completed_at="2026-06-30T00:01:00",
        status="ok",
        input_token_count=100,
        output_token_count=50,
        retries=0,
    )
    runs = extdb.list_extraction_runs()
    assert runs[0]["status"] == "ok"
    assert runs[0]["input_token_count"] == 100
    assert runs[0]["output_token_count"] == 50


def test_extraction_run_with_warnings(extdb):
    run_id = extdb.start_extraction_run(
        kind="assertion",
        scope="conversation:1",
        provider="fixture",
        model=None,
        prompt_version="assertion-v1",
        started_at="2026-06-30T00:00:00",
    )
    extdb.finish_extraction_run(
        run_id,
        completed_at="2026-06-30T00:02:00",
        status="partial",
        warnings=["grounding failed for msg 5", "schema error on item 2"],
    )
    runs = extdb.list_extraction_runs()
    assert runs[0]["status"] == "partial"
    assert runs[0]["warnings"] == 2
    assert "grounding failed" in runs[0]["warning_summary"]


def test_extraction_runs_filter_by_kind(extdb):
    extdb.start_extraction_run(kind="segment", scope="all", provider="fixture",
                               model=None, prompt_version="v1", started_at="2026-01-01")
    extdb.start_extraction_run(kind="assertion", scope="all", provider="fixture",
                               model=None, prompt_version="v1", started_at="2026-01-02")
    segs = extdb.list_extraction_runs(kind="segment")
    assert len(segs) == 1
    assert segs[0]["kind"] == "segment"


# ---------------------------------------------------------------------------
# D10 draft-model resolution (R3, 2026-07-15 design-compliance review)
# ---------------------------------------------------------------------------

def test_resolve_chat_model_defaults_to_14b(monkeypatch):
    """The shared draft/synthesis default is the 14b model (D10), used by
    weekly review, MCP context pack, and Health AI interpretation alike."""
    from app.llm import ollama
    monkeypatch.delenv("CAIRN_OLLAMA_MODEL", raising=False)
    assert ollama.resolve_chat_model() == ollama.CHAT_DEFAULT_MODEL == "qwen2.5:14b-instruct-q4_K_M"


def test_resolve_chat_model_env_override(monkeypatch):
    from app.llm import ollama
    monkeypatch.setenv("CAIRN_OLLAMA_MODEL", "qwen2.5:32b-instruct-q4_K_M")
    assert ollama.resolve_chat_model() == "qwen2.5:32b-instruct-q4_K_M"
    # explicit arg beats the env var
    assert ollama.resolve_chat_model("custom:latest") == "custom:latest"


def test_draft_paths_share_the_14b_default(monkeypatch):
    """weekly review and MCP context pack resolve to the same D10 default as
    resolve_chat_model — one contract, not three divergent constants."""
    monkeypatch.delenv("CAIRN_OLLAMA_MODEL", raising=False)
    from app.deliver import weekly_review
    from app.mcp import pack
    from app.llm import ollama
    assert weekly_review.DEFAULT_MODEL == ollama.CHAT_DEFAULT_MODEL
    assert pack.DEFAULT_MODEL == ollama.CHAT_DEFAULT_MODEL
