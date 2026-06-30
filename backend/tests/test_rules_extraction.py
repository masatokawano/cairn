"""Tests for P3-B: rules-based entity extraction (URL + GitHub repo)."""
import importlib

import pytest

from app.extraction.rules.urls import extract_urls, _normalise_url
from app.extraction.rules.github import extract_repos


# ---------------------------------------------------------------------------
# URL detector
# ---------------------------------------------------------------------------

def test_extract_urls_basic():
    text = "Check https://example.com for details."
    matches = extract_urls(text)
    assert len(matches) == 1
    m = matches[0]
    assert m.kind == "url"
    assert m.canonical_name == "https://example.com"
    assert m.external_id == "example.com"
    assert m.surface == "https://example.com"
    assert text[m.start:m.end] == m.surface


def test_extract_urls_strips_utm():
    text = "https://blog.example.com/post?utm_source=twitter&utm_medium=social"
    matches = extract_urls(text)
    assert len(matches) == 1
    assert "utm" not in matches[0].canonical_name
    assert matches[0].canonical_name == "https://blog.example.com/post"


def test_extract_urls_preserves_non_tracking_params():
    text = "https://example.com/search?q=python&page=2"
    matches = extract_urls(text)
    assert len(matches) == 1
    assert "q=python" in matches[0].canonical_name


def test_extract_urls_strips_trailing_punctuation():
    for punct in [".", ",", ")", "。", "，"]:
        text = f"see https://example.com{punct}"
        matches = extract_urls(text)
        assert len(matches) == 1, f"failed for punct={punct!r}"
        assert matches[0].canonical_name == "https://example.com"


def test_extract_urls_multiple():
    text = "See https://alpha.com and https://beta.org/path for more."
    matches = extract_urls(text)
    assert len(matches) == 2
    names = {m.canonical_name for m in matches}
    assert "https://alpha.com" in names
    assert "https://beta.org/path" in names


def test_extract_urls_none_in_plain_text():
    assert extract_urls("No URLs here, just plain text.") == []


def test_extract_urls_offsets_correct():
    prefix = "Visit "
    url = "https://example.com"
    text = prefix + url
    matches = extract_urls(text)
    assert len(matches) == 1
    assert matches[0].start == len(prefix)
    assert matches[0].end == len(text)


def test_normalise_url_lowercase_host():
    assert _normalise_url("HTTPS://EXAMPLE.COM/Path") == "https://example.com/Path"


def test_normalise_url_strips_trailing_slash():
    assert _normalise_url("https://example.com/") == "https://example.com"


def test_normalise_url_rejects_non_http():
    assert _normalise_url("ftp://files.example.com") is None


# ---------------------------------------------------------------------------
# GitHub repo detector
# ---------------------------------------------------------------------------

def test_extract_repos_basic():
    text = "Check out https://github.com/openai/whisper for ASR."
    matches = extract_repos(text)
    assert len(matches) == 1
    m = matches[0]
    assert m.kind == "repo"
    assert m.canonical_name == "https://github.com/openai/whisper"
    assert m.external_id == "openai/whisper"
    assert m.detector == "rules-repo-v1"


def test_extract_repos_without_https():
    text = "See github.com/huggingface/transformers for details."
    matches = extract_repos(text)
    assert len(matches) == 1
    assert matches[0].external_id == "huggingface/transformers"


def test_extract_repos_strips_git_suffix():
    text = "git clone https://github.com/foo/bar.git"
    matches = extract_repos(text)
    assert len(matches) == 1
    assert matches[0].external_id == "foo/bar"


def test_extract_repos_skips_builtin_paths():
    text = "https://github.com/orgs/myorg/teams"
    matches = extract_repos(text)
    assert len(matches) == 0


def test_extract_repos_skips_sub_paths():
    text = "https://github.com/foo/bar/blob/main/README.md"
    matches = extract_repos(text)
    # matches foo/bar but NOT foo/blob
    assert len(matches) == 1
    assert matches[0].external_id == "foo/bar"


def test_extract_repos_multiple():
    text = "I use github.com/pallets/flask and github.com/django/django."
    matches = extract_repos(text)
    assert len(matches) == 2
    ids = {m.external_id for m in matches}
    assert ids == {"pallets/flask", "django/django"}


def test_extract_repos_none_in_plain_text():
    assert extract_repos("No repos mentioned here.") == []


def test_extract_repos_offsets():
    prefix = "Repo: "
    url = "github.com/foo/bar"
    text = prefix + url + " is great"
    matches = extract_repos(text)
    assert len(matches) == 1
    assert matches[0].start == len(prefix)


# ---------------------------------------------------------------------------
# DB integration: upsert_entity / upsert_entity_mention
# ---------------------------------------------------------------------------

@pytest.fixture()
def edb(tmp_path, monkeypatch):
    monkeypatch.setenv("CAIRN_DB", str(tmp_path / "test.db"))
    from app import db as db_module
    importlib.reload(db_module)
    yield db_module
    db_module._local.__dict__.clear()


def test_upsert_entity_idempotent(edb):
    id1 = edb.upsert_entity(kind="url", canonical_name="https://example.com",
                             external_id="example.com", created_at="2026-01-01")
    id2 = edb.upsert_entity(kind="url", canonical_name="https://example.com",
                             external_id="example.com", created_at="2026-01-01")
    assert id1 == id2
    assert edb.count_entities(kind="url") == 1


def test_upsert_entity_different_kinds(edb):
    edb.upsert_entity(kind="url", canonical_name="https://github.com/foo/bar",
                      created_at="2026-01-01")
    edb.upsert_entity(kind="repo", canonical_name="https://github.com/foo/bar",
                      external_id="foo/bar", created_at="2026-01-01")
    assert edb.count_entities() == 2


def _seed_conv_and_msg(edb) -> tuple[int, int]:
    """Insert a minimal conversation + message and return (conv_id, msg_id)."""
    from app.parsers.base import ParsedConversation, ParsedMessage
    conv = ParsedConversation(
        source="chatgpt", source_id="c-test", title="test",
        messages=[ParsedMessage(role="user", text="hello", created_at="2025-01-01T00:00:00Z")],
        created_at="2025-01-01T00:00:00Z", updated_at="2025-01-01T00:00:00Z",
    )
    edb.upsert_conversations([conv])
    conn = edb.connect()
    conv_id = conn.execute("SELECT id FROM conversations LIMIT 1").fetchone()[0]
    msg_id = conn.execute("SELECT id FROM messages LIMIT 1").fetchone()[0]
    return conv_id, msg_id


def test_upsert_entity_mention_idempotent(edb):
    conv_id, msg_id = _seed_conv_and_msg(edb)
    eid = edb.upsert_entity(kind="url", canonical_name="https://example.com",
                             created_at="2026-01-01")
    kwargs = dict(entity_id=eid, message_id=msg_id, conversation_id=conv_id,
                  start_offset=0, end_offset=19, surface="https://example.com",
                  detector="rules-url-v1", created_at="2026-01-01")
    edb.upsert_entity_mention(**kwargs)
    edb.upsert_entity_mention(**kwargs)  # second call must not add a row
    assert edb.count_entity_mentions() == 1


def test_orphan_entity_mentions_empty(edb):
    assert edb.orphan_entity_mentions() == []


# ---------------------------------------------------------------------------
# rules_runner end-to-end
# ---------------------------------------------------------------------------

def test_rules_runner_basic(edb, monkeypatch):
    """Runner finds URLs and repos in a seeded conversation."""
    conv_id, msg_id = _seed_conv_and_msg(edb)
    # Update the message to contain URLs.
    edb.connect().execute(
        "UPDATE messages SET text=? WHERE id=?",
        ("See https://example.com and github.com/foo/bar for details.", msg_id),
    )
    edb.connect().commit()

    from app.extraction import rules_runner
    importlib.reload(rules_runner)
    summary = rules_runner.run_rules_extraction()

    assert summary["conversations"] >= 1
    assert summary["messages"] >= 1
    assert edb.count_entities(kind="url") >= 1
    assert edb.count_entities(kind="repo") >= 1
    assert edb.count_entity_mentions() >= 2
    assert edb.orphan_entity_mentions() == []


def test_rules_runner_idempotent(edb, monkeypatch):
    """Running twice does not add duplicate rows."""
    conv_id, msg_id = _seed_conv_and_msg(edb)
    edb.connect().execute(
        "UPDATE messages SET text=? WHERE id=?",
        ("https://example.com", msg_id),
    )
    edb.connect().commit()

    from app.extraction import rules_runner
    importlib.reload(rules_runner)
    rules_runner.run_rules_extraction()
    mentions_after_first = edb.count_entity_mentions()
    rules_runner.run_rules_extraction()
    assert edb.count_entity_mentions() == mentions_after_first


def test_rules_runner_scoped_to_conversation(edb):
    """--conversation restricts processing to the specified id."""
    conv_id, msg_id = _seed_conv_and_msg(edb)
    edb.connect().execute(
        "UPDATE messages SET text=? WHERE id=?",
        ("https://only-this.com", msg_id),
    )
    edb.connect().commit()

    from app.extraction import rules_runner
    importlib.reload(rules_runner)
    summary = rules_runner.run_rules_extraction(conversation_id=conv_id)
    assert summary["conversations"] == 1
    # Non-existent conv → 0 conversations processed
    summary2 = rules_runner.run_rules_extraction(conversation_id=9999)
    assert summary2["conversations"] == 0


def test_rules_runner_records_extraction_run(edb):
    """run_rules_extraction records a row in extraction_runs."""
    _seed_conv_and_msg(edb)

    from app.extraction import rules_runner
    importlib.reload(rules_runner)
    summary = rules_runner.run_rules_extraction()
    runs = edb.list_extraction_runs(kind="rules-entity")
    assert len(runs) >= 1
    assert runs[0]["status"] in ("ok", "partial")
    assert runs[0]["id"] == summary["run_id"]
