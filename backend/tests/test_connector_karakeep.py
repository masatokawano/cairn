"""connectors/karakeep.py — mocked HTTP, no real keys (DESIGN.md §10).

Covers: pagination + mapping, incremental early-stop on the createdAt
cursor, content_hash skip on resync, failure → sync_state.last_error with
cursor preserved, redaction of external text, read-only-ness (GET only).
"""
import importlib
import json

import httpx
import pytest


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("CAIRN_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("CAIRN_KARAKEEP_URL", "https://karakeep.test")
    from app import db as db_module
    importlib.reload(db_module)
    yield db_module
    conn = getattr(db_module._local, "conn", None)
    if conn:
        conn.close()
        db_module._local.conn = None


def bookmark(id_, created, *, url=None, title=None, tags=(), note=None, text=None):
    content = (
        {"type": "link", "url": url, "title": title}
        if url else {"type": "text", "text": text}
    )
    return {
        "id": id_,
        "createdAt": created,
        "modifiedAt": created,
        "title": title,
        "note": note,
        "favourited": False,
        "archived": False,
        "tags": [{"id": f"tag{i}", "name": t} for i, t in enumerate(tags)],
        "content": content,
    }


def paged_transport(pages: dict, request_log: list):
    """pages: {cursor_or_None: {"bookmarks": [...], "nextCursor": ...}}"""
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"  # read-only connector (invariant 1)
        params = dict(request.url.params)
        cursor = params.get("cursor")
        request_log.append(cursor)
        return httpx.Response(200, json=pages[cursor])
    return httpx.Client(transport=httpx.MockTransport(handler))


def sync_karakeep(**kwargs):
    from app.connectors import karakeep
    return karakeep.sync(api_key="test-key", **kwargs)


B_OLD = bookmark("bm-1", "2026-06-01T00:00:00Z",
                 url="https://www.example.com/article?utm_source=tw",
                 title="Article", tags=("to-review", "ai"))
B_MID = bookmark("bm-2", "2026-06-15T00:00:00Z",
                 url="https://twitter.com/user/status/99?s=20&t=x", title="Tweet")
B_NEW = bookmark("bm-3", "2026-07-01T00:00:00Z", text="text bookmark 本文", tags=("memo",))


def test_first_sync_paginates_and_maps(db):
    log = []
    client = paged_transport({
        None: {"bookmarks": [B_NEW, B_MID], "nextCursor": "p2"},
        "p2": {"bookmarks": [B_OLD], "nextCursor": None},
    }, log)
    stats = sync_karakeep(client=client)

    assert stats["fetched"] == 3
    assert stats["inserted"] == 3
    assert log == [None, "p2"]

    rows = {r["external_id"]: r for r in db.connect().execute(
        "SELECT * FROM items WHERE source='karakeep'").fetchall()}
    assert set(rows) == {"bm-1", "bm-2", "bm-3"}
    assert all(r["kind"] == "bookmark" for r in rows.values())
    # url_norm applied (utm stripped, twitter → x.com)
    assert rows["bm-1"]["url_norm"] == "https://example.com/article"
    assert rows["bm-2"]["url_norm"] == "https://x.com/user/status/99"
    # tags land in meta; text bookmark keeps its text excerpt
    assert json.loads(rows["bm-1"]["meta"])["tags"] == ["to-review", "ai"]
    assert "本文" in json.loads(rows["bm-3"]["meta"])["text"]

    state = db.get_sync_state("karakeep")
    assert state["cursor"]["last_created_at"] == "2026-07-01T00:00:00Z"
    # a first sync has no cursor → it runs as a full sweep and records it
    assert state["cursor"]["last_full_sync_at"]
    assert state["last_error"] is None


def test_incremental_stops_at_cursor(db):
    log1 = []
    sync_karakeep(client=paged_transport({
        None: {"bookmarks": [B_MID], "nextCursor": "p2"},
        "p2": {"bookmarks": [B_OLD], "nextCursor": None},
    }, log1))

    # second sync: a new bookmark on page 1, old ones behind it; page "deep"
    # must never be requested (early stop on the boundary page).
    b_newer = bookmark("bm-9", "2026-07-02T00:00:00Z",
                       url="https://example.com/fresh", title="Fresh")
    log2 = []
    stats = sync_karakeep(client=paged_transport({
        None: {"bookmarks": [b_newer, B_MID], "nextCursor": "p2"},
        "p2": {"bookmarks": [B_OLD], "nextCursor": "deep"},
        "deep": {"bookmarks": [], "nextCursor": None},
    }, log2))

    assert log2 == [None, "p2"], "must stop after the first all-old page"
    assert stats["inserted"] == 1
    assert stats["skipped"] == 2  # boundary overlap re-upserted as skips
    assert db.get_sync_state("karakeep")["cursor"]["last_created_at"] == "2026-07-02T00:00:00Z"


def test_boundary_timestamp_run_spanning_pages_not_lost(db):
    """Codex M1 review should #2: a new bookmark sharing the cursor's exact
    createdAt but sitting on a LATER page must still be fetched — pagination
    only stops once a whole page is strictly older than the cursor."""
    sync_karakeep(client=paged_transport(
        {None: {"bookmarks": [B_MID, B_OLD], "nextCursor": None}}, []))
    assert db.get_sync_state("karakeep")["cursor"]["last_created_at"] == B_MID["createdAt"]

    same_ts = bookmark("bm-8", B_MID["createdAt"],
                       url="https://example.com/same-second", title="SameTS")
    log = []
    stats = sync_karakeep(client=paged_transport({
        None: {"bookmarks": [B_MID], "nextCursor": "p2"},
        "p2": {"bookmarks": [same_ts], "nextCursor": "p3"},
        "p3": {"bookmarks": [B_OLD], "nextCursor": None},
    }, log))

    assert log == [None, "p2", "p3"], "pages touching the boundary keep pagination alive"
    assert stats["inserted"] == 1  # bm-8 recovered from page 2
    assert db.connect().execute(
        "SELECT COUNT(*) FROM items WHERE external_id='bm-8'").fetchone()[0] == 1


def test_resync_unchanged_is_all_skips(db):
    pages = {None: {"bookmarks": [B_OLD, B_MID], "nextCursor": None}}
    sync_karakeep(client=paged_transport(dict(pages), []))
    stats = sync_karakeep(client=paged_transport(dict(pages), []), full=True)
    assert stats["inserted"] == 0 and stats["updated"] == 0
    assert stats["skipped"] == 2
    assert stats["links"] is None  # nothing changed → no link rebuild


def test_full_sweep_picks_up_edits(db):
    sync_karakeep(client=paged_transport(
        {None: {"bookmarks": [B_OLD], "nextCursor": None}}, []))
    edited = json.loads(json.dumps(B_OLD))
    edited["tags"].append({"id": "t9", "name": "new-tag"})
    stats = sync_karakeep(client=paged_transport(
        {None: {"bookmarks": [edited], "nextCursor": None}}, []), full=True)
    assert stats["updated"] == 1
    meta = json.loads(db.connect().execute(
        "SELECT meta FROM items WHERE source='karakeep' AND external_id='bm-1'"
    ).fetchone()["meta"])
    assert "new-tag" in meta["tags"]


def test_http_error_records_last_error_and_keeps_cursor(db):
    sync_karakeep(client=paged_transport(
        {None: {"bookmarks": [B_OLD], "nextCursor": None}}, []))
    cursor_before = db.get_sync_state("karakeep")["cursor"]

    def failing(request):
        return httpx.Response(500, text="boom")
    with pytest.raises(httpx.HTTPStatusError):
        sync_karakeep(client=httpx.Client(transport=httpx.MockTransport(failing)))

    state = db.get_sync_state("karakeep")
    assert "500" in state["last_error"]
    assert state["cursor"] == cursor_before, "failed sync must not advance the cursor"
    # a later successful sync clears the error
    sync_karakeep(client=paged_transport(
        {None: {"bookmarks": [B_OLD], "nextCursor": None}}, []))
    assert db.get_sync_state("karakeep")["last_error"] is None


def test_missing_base_url_raises(db, monkeypatch):
    monkeypatch.delenv("CAIRN_KARAKEEP_URL")
    from app.connectors import ConnectorError
    with pytest.raises(ConnectorError):
        sync_karakeep(client=httpx.Client(transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"bookmarks": []}))))


def test_full_sweep_prunes_deleted_bookmarks(db):
    """レビュー指摘 3.4: full sweep が完全成功したときだけ、上流で削除された
    ブックマークを registry から prune する。"""
    sync_karakeep(client=paged_transport(
        {None: {"bookmarks": [B_OLD, B_MID], "nextCursor": None}}, []))
    # bm-1 deleted upstream; the full sweep only returns bm-2
    stats = sync_karakeep(client=paged_transport(
        {None: {"bookmarks": [B_MID], "nextCursor": None}}, []), full=True)
    assert stats["pruned"] == 1
    ids = {r["external_id"] for r in db.connect().execute(
        "SELECT external_id FROM items WHERE source='karakeep'")}
    assert ids == {"bm-2"}


def test_incremental_never_prunes(db):
    sync_karakeep(client=paged_transport(
        {None: {"bookmarks": [B_OLD, B_MID], "nextCursor": None}}, []))
    b_newer = bookmark("bm-9", "2026-07-02T00:00:00Z",
                       url="https://example.com/fresh", title="Fresh")
    # last_full_sync_at is fresh → this run stays incremental even though
    # the page no longer contains the old bookmarks
    stats = sync_karakeep(client=paged_transport(
        {None: {"bookmarks": [b_newer], "nextCursor": None}}, []))
    assert stats["full"] is False and stats["pruned"] == 0
    count = db.connect().execute(
        "SELECT COUNT(*) FROM items WHERE source='karakeep'").fetchone()[0]
    assert count == 3  # bm-1 / bm-2 still there


def test_full_sweep_auto_promotes_after_interval(db):
    """レビュー指摘 3.2: 古いブックマークの編集・削除は増分同期に映らないため、
    last_full_sync_at が閾値を超えたら通常 sync が full sweep へ自動昇格する。"""
    sync_karakeep(client=paged_transport(
        {None: {"bookmarks": [B_OLD, B_MID], "nextCursor": None}}, []))
    state = db.get_sync_state("karakeep")
    db.set_sync_state("karakeep", cursor={
        "last_created_at": state["cursor"]["last_created_at"],
        "last_full_sync_at": "2026-07-01T00:00:00Z",  # 閾値超え（過去）
    })
    stats = sync_karakeep(client=paged_transport(
        {None: {"bookmarks": [B_MID], "nextCursor": None}}, []))  # no full=True
    assert stats["full"] is True
    assert stats["pruned"] == 1  # bm-1 removed upstream → pruned
    assert db.get_sync_state("karakeep")["cursor"]["last_full_sync_at"] \
        > "2026-07-01T00:00:00Z"


def test_empty_full_listing_does_not_prune(db):
    sync_karakeep(client=paged_transport(
        {None: {"bookmarks": [B_OLD], "nextCursor": None}}, []))
    stats = sync_karakeep(client=paged_transport(
        {None: {"bookmarks": [], "nextCursor": None}}, []), full=True)
    assert stats["pruned"] == 0
    count = db.connect().execute(
        "SELECT COUNT(*) FROM items WHERE source='karakeep'").fetchone()[0]
    assert count == 1


def test_external_text_is_redacted(db):
    leaky = bookmark("bm-7", "2026-06-20T00:00:00Z",
                     url="https://example.com/k",
                     title="key sk-ant-api03-abcdefghijklmnopqrstuvwx note",
                     note="token sk-ant-api03-abcdefghijklmnopqrstuvwx here")
    sync_karakeep(client=paged_transport(
        {None: {"bookmarks": [leaky], "nextCursor": None}}, []))
    row = db.connect().execute(
        "SELECT title, meta FROM items WHERE external_id='bm-7'").fetchone()
    assert "sk-ant-" not in row["title"] and "sk-ant-" not in row["meta"]
    assert "[REDACTED:anthropic]" in row["meta"]
