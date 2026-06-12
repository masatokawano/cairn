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
