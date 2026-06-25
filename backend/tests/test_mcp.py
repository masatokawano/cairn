"""Tests for the MCP server tools (called via the FastMCP in-memory API)."""
import importlib

import pytest


@pytest.fixture()
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("CAIRN_DB", str(tmp_path / "t.db"))
    from app import db, mcp_server
    importlib.reload(db)
    importlib.reload(mcp_server)
    from app.parsers.base import ParsedConversation, ParsedMessage

    convs = [
        ParsedConversation(
            source="claude_cli", source_id=f"s{i}", title=f"MCP STDIOの脆弱性調査 {i}",
            messages=[
                ParsedMessage(role="user", text=f"MCP STDIOの脆弱性について調べて ({i})",
                              created_at=f"2026-06-0{(i % 9) + 1}T10:00:00Z"),
                ParsedMessage(role="assistant", text="結論: STDIOはローカル実行のため攻撃面は限定的",
                              created_at=f"2026-06-0{(i % 9) + 1}T10:01:00Z"),
            ],
            created_at=f"2026-06-0{(i % 9) + 1}T10:00:00Z",
            updated_at=f"2026-06-0{(i % 9) + 1}T10:01:00Z",
            meta={"cwd": "/Users/test/proj"},
        )
        for i in range(15)
    ]
    db.upsert_conversations(convs)
    yield mcp_server, db
    conn = getattr(db._local, "conn", None)
    if conn:
        conn.close()
        db._local.conn = None


def test_search_caps_at_10_and_paginates(setup):
    mcp_server, _ = setup
    r = mcp_server.search_conversations(query="脆弱性", limit=50)
    assert r["count"] == 10  # capped
    assert r["has_more"] is True
    assert r["next_offset"] == 10
    r2 = mcp_server.search_conversations(query="脆弱性", offset=r["next_offset"])
    assert r2["count"] == 5
    assert r2["has_more"] is False
    ids = {x["conversation_id"] for x in r["results"]} | {x["conversation_id"] for x in r2["results"]}
    assert len(ids) == 15


def test_search_snippets_fenced_and_capped(setup):
    mcp_server, _ = setup
    r = mcp_server.search_conversations(query="脆弱性", limit=1)
    snip = r["results"][0]["snippet"]
    assert snip.startswith(mcp_server.DATA_OPEN)
    assert snip.endswith(mcp_server.DATA_CLOSE)
    body = snip[len(mcp_server.DATA_OPEN):-len(mcp_server.DATA_CLOSE)]
    assert len(body) <= mcp_server.MAX_SNIPPET + 2  # +newlines


def test_search_filters(setup):
    mcp_server, _ = setup
    assert mcp_server.search_conversations(query="脆弱性", source="chatgpt")["count"] == 0
    assert "error" in mcp_server.search_conversations(query="x", source="bogus")
    r = mcp_server.search_conversations(query="脆弱性", after="2026-06-09")
    assert 0 < r["count"] < 10  # only conversations updated on/after 06-09


def test_get_conversation_chunks_long_threads(setup, monkeypatch):
    mcp_server, db = setup
    from app.parsers.base import ParsedConversation, ParsedMessage
    db.upsert_conversations([ParsedConversation(
        source="codex_cli", source_id="long", title="長い会話",
        messages=[ParsedMessage(role="user", text=f"メッセージ{i} " + "あ" * 300) for i in range(6)],
    )])
    conv_id = db.connect().execute(
        "SELECT id FROM conversations WHERE source_id='long'"
    ).fetchone()["id"]
    monkeypatch.setattr(mcp_server, "MAX_BODY_CHARS", 700)

    r = mcp_server.get_conversation(conversation_id=conv_id)
    assert r["total_messages"] == 6
    assert r["has_more"] is True
    assert 0 < len(r["messages"]) < 6
    assert all(m["text"].startswith(mcp_server.DATA_OPEN) for m in r["messages"])

    # walk the continuation to the end; indexes must cover 0..5 exactly once
    indexes = [m["index"] for m in r["messages"]]
    guard = 0
    while r["has_more"]:
        r = mcp_server.get_conversation(conversation_id=conv_id, start_message=r["next_start_message"])
        indexes += [m["index"] for m in r["messages"]]
        guard += 1
        assert guard < 10
    assert indexes == [0, 1, 2, 3, 4, 5]


def test_get_conversation_not_found(setup):
    mcp_server, _ = setup
    assert "error" in mcp_server.get_conversation(conversation_id=9999)


def test_list_recent_filters_by_window(setup, monkeypatch):
    mcp_server, db = setup
    # everything in the fixture is 2026-06-0x; "now" is later — use a big window
    r = mcp_server.list_recent_conversations(days=36500, limit=50)
    assert r["count"] == 15
    r = mcp_server.list_recent_conversations(days=36500, limit=3)
    assert r["count"] == 3
    assert "error" in mcp_server.list_recent_conversations(source="bogus")


@pytest.mark.anyio
async def test_tools_registered_with_schemas(setup):
    mcp_server, _ = setup
    tools = await mcp_server.mcp.list_tools()
    names = {t.name for t in tools}
    assert names == {"search_conversations", "get_conversation", "list_recent_conversations"}
    for t in tools:
        assert t.description, f"{t.name} missing description"
    search_tool = next(t for t in tools if t.name == "search_conversations")
    assert "query" in search_tool.inputSchema["properties"]


@pytest.fixture
def anyio_backend():
    return "asyncio"


# --- P2-4: mode parameter on search_conversations ---------------------------

def test_search_default_mode_is_keyword_with_back_compat(setup):
    """Calling without `mode=` must keep working — same hit count, same shape,
    plus the new fields. Existing MCP clients that haven't been updated must
    not regress."""
    mcp_server, _ = setup
    r = mcp_server.search_conversations(query="脆弱性")
    assert r["mode"] == "keyword"
    assert r["count"] > 0
    h = r["results"][0]
    assert h["match_reason"] == "keyword"
    assert h["semantic_score"] is None
    # message_id is new — needed for "jump to matched message" downstream.
    assert isinstance(h["message_id"], int)


def test_search_rejects_invalid_mode(setup):
    mcp_server, _ = setup
    r = mcp_server.search_conversations(query="x", mode="fuzzy")
    assert "error" in r and "invalid mode" in r["error"]


def test_search_semantic_without_embeddings_returns_error_not_500(setup):
    """If the user picks semantic before `admin reindex`, the MCP client
    needs an actionable message — not a transport-level failure."""
    mcp_server, _ = setup
    r = mcp_server.search_conversations(query="脆弱性", mode="semantic")
    assert "error" in r
    assert "admin reindex" in r["error"]


def test_search_semantic_mode_uses_embeddings(setup, monkeypatch):
    """End-to-end semantic path through MCP: embed chunks with a fixture
    provider, set CAIRN_EMBED_PROVIDER so the resolver finds it via the
    same name in the embeddings table, then verify mode=semantic returns
    cosine-ranked results with match_reason='semantic'."""
    import hashlib
    import math
    from app.embedding import EmbeddingProvider, vector_to_bytes

    class FixtureProvider(EmbeddingProvider):
        name = "local-sbert"  # use a known name so _active_embedding_provider
        model = "fake-test-model"  # finds something to construct — but we
        dimension = 8  # short-circuit construction below.

        def _vec(self, text):
            h = hashlib.sha256(text.encode("utf-8")).digest()
            raw = [b - 128 for b in h[:8]]
            norm = math.sqrt(sum(x * x for x in raw)) or 1.0
            return [x / norm for x in raw]

        def embed_passages(self, texts):
            return [vector_to_bytes(self._vec(t)) for t in texts]

        def embed_query(self, text):
            return vector_to_bytes(self._vec(text))

    mcp_server, db = setup
    fp = FixtureProvider()
    db.embed_chunks(fp)
    # Patch the resolver to return our fixture instead of loading e5-small.
    monkeypatch.setattr(db, "_active_embedding_provider", lambda: fp)

    r = mcp_server.search_conversations(query="脆弱性について", mode="semantic", limit=3)
    assert r["mode"] == "semantic"
    assert r["count"] > 0
    h = r["results"][0]
    assert h["match_reason"] == "semantic"
    assert isinstance(h["semantic_score"], float)
    # Semantic-only hits have no FTS highlights → empty matched_keywords.
    assert h["matched_keywords"] == []


def test_search_hybrid_mode_marks_dual_hits(setup, monkeypatch):
    """A conversation hit by *both* paths should be tagged match_reason=both
    so the LLM client can explain to the user that the result is doubly
    grounded (literal match + semantic similarity)."""
    import hashlib
    import math
    from app.embedding import EmbeddingProvider, vector_to_bytes

    class FixtureProvider(EmbeddingProvider):
        name = "local-sbert"
        model = "fake-test-model"
        dimension = 8

        def _vec(self, text):
            h = hashlib.sha256(text.encode("utf-8")).digest()
            raw = [b - 128 for b in h[:8]]
            norm = math.sqrt(sum(x * x for x in raw)) or 1.0
            return [x / norm for x in raw]

        def embed_passages(self, texts):
            return [vector_to_bytes(self._vec(t)) for t in texts]

        def embed_query(self, text):
            return vector_to_bytes(self._vec(text))

    mcp_server, db = setup
    fp = FixtureProvider()
    db.embed_chunks(fp)
    monkeypatch.setattr(db, "_active_embedding_provider", lambda: fp)

    r = mcp_server.search_conversations(query="脆弱性", mode="hybrid", limit=10)
    assert r["mode"] == "hybrid"
    assert r["count"] > 0
    # All 15 fixture conversations contain "脆弱性" so the keyword path hits
    # them all; the semantic path also embeds the same exact strings → most
    # results should be tagged "both".
    reasons = {h["match_reason"] for h in r["results"]}
    assert "both" in reasons


@pytest.mark.anyio
async def test_search_tool_schema_includes_mode(setup):
    """The mode parameter must appear in the tool's inputSchema so MCP
    clients (and IDE auto-complete) know it's an option."""
    mcp_server, _ = setup
    tools = await mcp_server.mcp.list_tools()
    search_tool = next(t for t in tools if t.name == "search_conversations")
    assert "mode" in search_tool.inputSchema["properties"]
