"""Tests for the M5 cross-source MCP server (called via the in-memory tool API).

Covers the four §5.6 tools over the unified items registry: search_all
(cross-kind/source, caps, fencing, filters, modes), get_item (conversation
body + external item), build_context_pack (構想/根拠/過去の議論 buckets, the
strong-link 根拠 augmentation, §6.2 content/synthesized separation and the
opt-in/degrading LLM draft) and get_recent_activity.
"""
import importlib

import pytest

AUTH_URL = "https://example.com/oauth-spec"  # linked from the auth conversation


@pytest.fixture()
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("CAIRN_DB", str(tmp_path / "t.db"))
    from app import db
    importlib.reload(db)  # in-place: recall/server/pack keep valid db refs
    from app import recall
    from app.mcp import pack, server
    from app.parsers.base import ParsedConversation, ParsedMessage

    # 15 conversations on one topic — the cap/pagination corpus.
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
    # One 認証設計 conversation that cites AUTH_URL — seeds 構想 and links to
    # the oauth bookmark for the 根拠 augmentation path.
    convs.append(ParsedConversation(
        source="claude_cli", source_id="auth", title="認証設計の議論",
        messages=[
            ParsedMessage(role="user",
                          text=f"認証設計について議論する。参考 {AUTH_URL} を読んだ",
                          created_at="2026-06-15T10:00:00Z"),
            ParsedMessage(role="assistant", text="トークン失効とリフレッシュが論点",
                          created_at="2026-06-15T10:01:00Z"),
        ],
        created_at="2026-06-15T10:00:00Z", updated_at="2026-06-15T10:02:00Z",
        meta={"cwd": "/Users/test/proj"},
    ))
    db.upsert_conversations(convs)

    # A Karakeep bookmark whose TITLE does not contain 認証設計 — it can only
    # enter 根拠 via the item_link from the auth conversation, not via search.
    db.upsert_items("karakeep", "bookmark", [{
        "external_id": "bm-oauth", "title": "OAuth 2.1 draft", "url": AUTH_URL,
        "created_at": "2026-07-01T00:00:00Z", "updated_at": "2026-07-01T00:00:00Z",
        "meta": {"description": "oauth authorization framework", "tags": ["to-review"]},
    }])
    # A Zotero reference that DOES match 認証設計 — a direct 根拠 seed.
    db.upsert_items("zotero", "reference", [{
        "external_id": "ref-auth", "title": "認証設計の形式検証", "url": None,
        "created_at": "2026-05-01T00:00:00Z", "updated_at": "2026-05-01T00:00:00Z",
        "meta": {"abstract": "認証設計を形式手法で検証する", "creators": ["Y. Ando"]},
    }])
    db.rechunk_items(force=True)      # index external items into chunks_fts
    db.rebuild_item_links()           # auth conversation ↔ oauth bookmark (url)

    yield server, pack, recall, db
    conn = getattr(db._local, "conn", None)
    if conn:
        conn.close()
        db._local.conn = None


@pytest.fixture
def anyio_backend():
    return "asyncio"


# --- search_all --------------------------------------------------------------

def test_search_all_caps_at_10_and_paginates(setup):
    server, *_ = setup
    r = server.search_all(query="脆弱性", k=50)
    assert r["count"] == 10 and r["has_more"] is True and r["next_offset"] == 10
    r2 = server.search_all(query="脆弱性", offset=10)
    assert r2["count"] == 5 and r2["has_more"] is False
    ids = {x["conversation_id"] for x in r["results"] + r2["results"]}
    assert len(ids) == 15


def test_search_all_snippets_fenced_and_capped(setup):
    server, *_ = setup
    from app import mcp as mcpkg
    h = server.search_all(query="脆弱性", k=1)["results"][0]
    assert h["snippet"].startswith(mcpkg.DATA_OPEN)
    assert h["snippet"].endswith(mcpkg.DATA_CLOSE)
    body = h["snippet"][len(mcpkg.DATA_OPEN):-len(mcpkg.DATA_CLOSE)]
    assert len(body) <= mcpkg.MAX_SNIPPET + 2
    # titles are external text too — fenced like snippets (§6.1)
    assert h["title"].startswith(mcpkg.DATA_OPEN)
    assert h["title"].endswith(mcpkg.DATA_CLOSE)


def test_search_all_kind_and_source_filters(setup):
    server, *_ = setup
    # reference kind matches 認証設計 (title); conversation kind must not.
    refs = server.search_all(query="認証設計", kinds=["reference"])
    assert refs["count"] >= 1
    assert {x["kind"] for x in refs["results"]} == {"reference"}
    convs = server.search_all(query="認証設計", kinds=["conversation"])
    assert all(x["kind"] == "conversation" for x in convs["results"])
    assert convs["count"] >= 1
    # provenance fields present on a cross-source result
    row = refs["results"][0]
    assert row["source"] == "zotero" and row["external_id"] == "ref-auth"


def test_search_all_rejects_bad_filters_and_mode(setup):
    server, *_ = setup
    assert "error" in server.search_all(query="x", source="bogus")
    assert "error" in server.search_all(query="x", kinds=["bogus"])
    assert "error" in server.search_all(query="x", mode="fuzzy")


def test_search_all_semantic_without_embeddings_is_error_not_crash(setup):
    server, *_ = setup
    r = server.search_all(query="脆弱性", mode="semantic")
    assert "error" in r and "admin reindex" in r["error"]


# --- get_item ----------------------------------------------------------------

def test_get_item_conversation_returns_full_body(setup, monkeypatch):
    server, pack, recall, db = setup
    from app import mcp as mcpkg
    r = server.get_item(source="claude_cli", external_id="auth")
    assert r["kind"] == "conversation"
    assert r["source"] == "claude_cli" and r["external_id"] == "auth"
    assert r["total_messages"] == 2
    assert all(m["text"].startswith(mcpkg.DATA_OPEN) for m in r["messages"])
    assert r["title"].startswith(mcpkg.DATA_OPEN)  # conversation title fenced


def test_get_item_conversation_paginates_long_thread(setup, monkeypatch):
    server, pack, recall, db = setup
    from app import mcp as mcpkg
    from app.parsers.base import ParsedConversation, ParsedMessage
    db.upsert_conversations([ParsedConversation(
        source="codex_cli", source_id="long", title="長い会話",
        messages=[ParsedMessage(role="user", text=f"メッセージ{i} " + "あ" * 300)
                  for i in range(6)],
    )])
    monkeypatch.setattr(server, "MAX_BODY_CHARS", 700)
    r = server.get_item(source="codex_cli", external_id="long")
    assert r["has_more"] is True
    indexes = [m["index"] for m in r["messages"]]
    guard = 0
    while r["has_more"]:
        r = server.get_item(source="codex_cli", external_id="long",
                            start_message=r["next_start_message"])
        indexes += [m["index"] for m in r["messages"]]
        guard += 1
        assert guard < 10
    assert indexes == [0, 1, 2, 3, 4, 5]


def test_get_item_external_returns_meta_and_body(setup):
    server, *_ = setup
    from app import mcp as mcpkg
    r = server.get_item(source="zotero", external_id="ref-auth")
    assert r["kind"] == "reference" and r["source"] == "zotero"
    assert r["title"].startswith(mcpkg.DATA_OPEN)
    assert r["body"].startswith(mcpkg.DATA_OPEN)
    assert "認証設計" in r["body"]
    # free-text meta (abstract/creators …) is inside the fenced body, not
    # returned as bare structured fields (§6.2 構造的分離)
    assert "Y. Ando" in r["body"]
    assert "creators" not in r["meta"] and "abstract" not in r["meta"]


def test_get_item_meta_is_whitelist_projected(setup):
    """Karakeep description/note/tags must never reach the model unfenced via
    meta — only machine-ish whitelist fields survive the projection."""
    server, *_ = setup
    r = server.get_item(source="karakeep", external_id="bm-oauth")
    assert "description" not in r["meta"] and "tags" not in r["meta"]
    assert set(r["meta"]) <= set(server._META_WHITELIST)
    assert "oauth authorization framework" in r["body"]  # still available, fenced


def test_get_item_not_found_and_bad_source(setup):
    server, *_ = setup
    assert "error" in server.get_item(source="zotero", external_id="nope")
    assert "error" in server.get_item(source="bogus", external_id="x")


# --- build_context_pack ------------------------------------------------------

def test_context_pack_buckets_and_link_evidence(setup):
    server, pack, recall, db = setup
    from app import mcp as mcpkg
    r = server.build_context_pack(topic="認証設計")
    content = r["content"]
    assert set(content) == {"vision", "evidence", "past_discussion"}
    # 構想: the auth conversation
    vision_convs = [c for c in content["vision"] if c["kind"] == "conversation"]
    assert any(c["external_id"] == "auth" for c in vision_convs)
    # 根拠: the direct reference seed AND the oauth bookmark reached only via
    # the item_link (its title does not match 認証設計).
    ev_ids = {c["external_id"] for c in content["evidence"]}
    assert "ref-auth" in ev_ids
    assert "bm-oauth" in ev_ids
    oauth = next(c for c in content["evidence"] if c["external_id"] == "bm-oauth")
    assert oauth["link_via"] == "url"
    # provenance: titles are fenced untrusted data
    assert content["vision"][0]["title"].startswith(mcpkg.DATA_OPEN)
    # §6.2 separation: synthesized is opt-in, off by default
    assert r["synthesized"] is None
    assert "synthesize=true" in r["synthesis_note"]


def test_context_pack_empty_topic_errors(setup):
    server, *_ = setup
    assert "error" in server.build_context_pack(topic="   ")


def test_context_pack_synthesize_labels_draft(setup):
    _, pack, _, _ = setup
    from app.llm.fixture import FixtureProvider
    draft = {"vision": ["トークン設計を再考"], "evidence": ["OAuth 2.1 draft"],
             "past_discussion": ["失効方式の議論"], "open_questions": ["回転周期は?"]}
    llm = FixtureProvider(responses=[draft])
    r = pack.build_context_pack("認証設計", synthesize=True, llm=llm)
    syn = r["synthesized"]
    assert syn is not None
    assert syn["generated_by"] == f"cairn/fixture-v1/{pack.PROMPT_VERSION}"
    assert "## 未解決課題" in syn["text"] and "回転周期は?" in syn["text"]
    assert r["synthesis_note"] is None


def test_context_pack_synthesize_degrades_on_llm_failure(setup):
    _, pack, _, _ = setup
    from app.llm.fixture import FixtureProvider
    llm = FixtureProvider(fail_first=99)  # always raises
    r = pack.build_context_pack("認証設計", synthesize=True, llm=llm)
    assert r["synthesized"] is None
    assert "失敗" in r["synthesis_note"]
    # content is unaffected by the synthesis failure (S4)
    assert set(r["content"]) == {"vision", "evidence", "past_discussion"}


def test_context_pack_past_discussion_projects_reason(setup, monkeypatch):
    """past_discussion projection + seed dedup, isolated from related()'s
    ranking: inject one older related row and one that duplicates a seed."""
    server, pack, recall, db = setup
    from app import mcp as mcpkg
    seed_iids = {c.get("item_id") for c in
                 server.build_context_pack(topic="認証設計")["content"]["vision"]}
    dup_iid = next(iter(seed_iids))
    fake = [
        {"item_id": 999999, "conversation_id": None, "kind": "conversation",
         "source": "chatgpt", "external_id": "old1", "title": "以前の認証議論",
         "url": None, "updated_at": "2026-01-01T00:00:00Z", "snippet": "旧ログ",
         "reason": {"query": "認証", "match_reason": "keyword"}},
        {"item_id": dup_iid, "conversation_id": None, "kind": "conversation",
         "source": "claude_cli", "external_id": "auth", "title": "dup",
         "url": None, "updated_at": "2026-01-01T00:00:00Z", "snippet": "x",
         "reason": {"query": "認証", "match_reason": "keyword"}},
    ]
    monkeypatch.setattr(recall, "related", lambda *a, **k: fake)
    pack_out = pack.build_context_pack("認証設計")
    past = pack_out["content"]["past_discussion"]
    ids = {p["external_id"] for p in past}
    assert "old1" in ids and "auth" not in ids  # dup seed filtered
    row = next(p for p in past if p["external_id"] == "old1")
    assert row["title"].startswith(mcpkg.DATA_OPEN)
    assert row["reason"]["match_reason"] == "keyword"


# --- get_recent_activity -----------------------------------------------------

def test_get_recent_activity_groups_and_filters(setup):
    server, *_ = setup
    from app import mcp as mcpkg
    r = server.get_recent_activity(days=36500)
    assert set(r) >= {"days", "since", "until", "discoveries", "thoughts",
                      "references", "notes"}
    # the oauth bookmark is a recent discovery; title fenced
    disc_ids = {d["external_id"] for d in r["discoveries"]}
    assert "bm-oauth" in disc_ids
    assert r["discoveries"][0]["title"].startswith(mcpkg.DATA_OPEN)
    # source filter keeps only that source's rows
    only = server.get_recent_activity(days=36500, source="zotero")
    assert all(x["source"] == "zotero" for x in only["references"])
    assert only["discoveries"] == []
    assert "error" in server.get_recent_activity(source="bogus")


# --- tool registration -------------------------------------------------------

@pytest.mark.anyio
async def test_four_tools_registered_with_schemas(setup):
    server, *_ = setup
    tools = await server.mcp.list_tools()
    names = {t.name for t in tools}
    assert names == {"search_all", "get_item", "build_context_pack",
                     "get_recent_activity"}
    for t in tools:
        assert t.description, f"{t.name} missing description"
    by = {t.name: t for t in tools}
    assert "kinds" in by["search_all"].inputSchema["properties"]
    assert "synthesize" in by["build_context_pack"].inputSchema["properties"]
