"""Redaction unit tests. All secrets here are format-matching DUMMY values."""
from app.redact import redact, redact_title, scan

# Dummy values, never real credentials.
FAKE_OPENAI = "sk-proj-DUMMYDUMMYDUMMYDUMMYDUMMY0000000000TESTTESTTESTTEST"
FAKE_ANTHROPIC = "sk-ant-api03-DUMMYDUMMYDUMMYDUMMYDUMMY0000000000TESTTEST"
FAKE_AWS = "AKIAIOSFODNN7EXAMPLE"  # AWS's official documentation example key id
FAKE_GHP = "ghp_" + "a1B2" * 9  # 36 chars
FAKE_GH_PAT = "github_pat_" + "ab12CD" * 5
FAKE_PEM = (
    "-----BEGIN RSA PRIVATE KEY-----\n"
    "MIIDUMMYDUMMYDUMMY\nDUMMYDUMMY==\n"
    "-----END RSA PRIVATE KEY-----"
)


def test_openai_redacted():
    out = redact(f"キーはこれ: {FAKE_OPENAI} です")
    assert FAKE_OPENAI not in out
    assert out == "キーはこれ: [REDACTED:openai] です"


def test_anthropic_not_mislabeled_as_openai():
    out = redact(f"use {FAKE_ANTHROPIC} here")
    assert out == "use [REDACTED:anthropic] here"


def test_aws_redacted():
    assert redact(f"id={FAKE_AWS}") == "id=[REDACTED:aws]"


def test_github_tokens_redacted():
    assert redact(FAKE_GHP) == "[REDACTED:github]"
    assert redact(FAKE_GH_PAT) == "[REDACTED:github]"


def test_pem_block_redacted():
    out = redact(f"before\n{FAKE_PEM}\nafter")
    assert "PRIVATE KEY" not in out
    assert "[REDACTED:private-key]" in out
    assert out.startswith("before") and out.endswith("after")


def test_pem_block_without_end_marker_redacted():
    # Truncated paste: redact to end of text rather than leaving the key
    out = redact("x\n-----BEGIN PRIVATE KEY-----\nMIIDUMMY")
    assert "MIIDUMMY" not in out


def test_clean_text_unchanged():
    text = "FTS5のtrigramはsk-学習やAKIAなど短い断片にはマッチしない。ghp_short も平気。"
    assert redact(text) == text
    assert scan(text) == {}


def test_multiple_secrets_counted_once_each():
    text = f"a {FAKE_OPENAI} b {FAKE_OPENAI} c {FAKE_ANTHROPIC} d {FAKE_AWS}"
    counts = scan(text)
    assert counts == {"openai": 2, "anthropic": 1, "aws": 1}


def test_title_truncated_secret_redacted():
    # Titles cut at ~60 chars can leave only a prefix of the key
    truncated = "APIキーを設定して: " + FAKE_OPENAI[:30] + "…"
    out = redact_title(truncated)
    assert FAKE_OPENAI[:30] not in out
    assert "[REDACTED:openai]" in out


def test_ingest_applies_redaction(tmp_path, monkeypatch):
    """End-to-end: secrets never reach the DB through upsert_conversations."""
    import importlib

    monkeypatch.setenv("CAIRN_DB", str(tmp_path / "t.db"))
    from app import db as db_module
    importlib.reload(db_module)
    try:
        from app.parsers.base import ParsedConversation, ParsedMessage

        pc = ParsedConversation(
            source="claude_cli",
            source_id="s1",
            title="キー設定: " + FAKE_OPENAI[:40],
            messages=[
                ParsedMessage(role="user", text=f"このキーを使って {FAKE_OPENAI}"),
                ParsedMessage(role="assistant", text="設定しました"),
            ],
        )
        db_module.upsert_conversations([pc])
        conv = db_module.get_conversation(1)
        assert FAKE_OPENAI not in conv["messages"][0]["text"]
        assert "[REDACTED:openai]" in conv["messages"][0]["text"]
        assert FAKE_OPENAI[:40] not in conv["title"]
        # searchable by the redaction marker, not by the secret
        assert db_module.search("REDACTED") != []
        assert db_module.search(FAKE_OPENAI[:20]) == []
        # stored hash matches redacted content → identical re-ingest skips
        pc2 = ParsedConversation(
            source="claude_cli", source_id="s1", title=pc.title,
            messages=[
                ParsedMessage(role="user", text=f"このキーを使って {FAKE_OPENAI}"),
                ParsedMessage(role="assistant", text="設定しました"),
            ],
        )
        assert db_module.upsert_conversations([pc2])["skipped"] == 1
    finally:
        conn = getattr(db_module._local, "conn", None)
        if conn:
            conn.close()
            db_module._local.conn = None
