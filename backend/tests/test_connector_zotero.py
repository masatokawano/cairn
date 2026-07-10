"""connectors/zotero.py — mocked HTTP, no real keys (DESIGN.md §10).

Covers: library-version cursor (since param + Last-Modified-Version),
start/limit pagination, bibliographic mapping incl. DOI normalisation,
attachment/note filtering, failure → last_error with cursor preserved.
"""
import importlib
import json

import httpx
import pytest


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("CAIRN_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("CAIRN_ZOTERO_USER_ID", "12345")
    from app import db as db_module
    importlib.reload(db_module)
    yield db_module
    conn = getattr(db_module._local, "conn", None)
    if conn:
        conn.close()
        db_module._local.conn = None


def zitem(key, *, title="論文", item_type="journalArticle", doi=None, url=None,
          abstract=None, creators=(), tags=(), modified="2026-06-20T10:00:00Z"):
    return {
        "key": key,
        "version": 40,
        "data": {
            "key": key,
            "itemType": item_type,
            "title": title,
            "DOI": doi,
            "url": url,
            "abstractNote": abstract,
            "creators": list(creators),
            "tags": [{"tag": t} for t in tags],
            "dateAdded": "2026-06-10T09:00:00Z",
            "dateModified": modified,
            "date": "2023",
            "publicationTitle": "Nature",
        },
    }


def transport(responder, request_log: list):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"  # read-only connector (invariant 1)
        assert request.headers["Zotero-API-Version"] == "3"
        params = dict(request.url.params)
        request_log.append(params)
        return responder(params)
    return httpx.Client(transport=httpx.MockTransport(handler))


def sync_zotero(**kwargs):
    from app.connectors import zotero
    return zotero.sync(api_key="test-key", **kwargs)


def test_first_sync_maps_bibliography(db):
    items = [
        zitem("KEY1", doi="10.1038/S41586-023-06004-9", abstract="概要テキスト",
              creators=[{"firstName": "Ada", "lastName": "Lovelace"},
                        {"name": "Some Consortium"}],
              tags=("ml", "biology")),
        zitem("KEY2", title="Webページ", item_type="webpage",
              url="https://doi.org/10.1000/xyz123"),
    ]
    log = []
    stats = sync_zotero(client=transport(
        lambda p: httpx.Response(200, json=items, headers={"Last-Modified-Version": "42"}),
        log))

    assert stats["inserted"] == 2
    assert "since" not in log[0], "first sync must not send a since param"

    rows = {r["external_id"]: r for r in db.connect().execute(
        "SELECT * FROM items WHERE source='zotero'").fetchall()}
    assert all(r["kind"] == "reference" for r in rows.values())
    assert rows["KEY1"]["doi"] == "10.1038/s41586-023-06004-9"  # lowercased
    meta1 = json.loads(rows["KEY1"]["meta"])
    assert meta1["creators"] == ["Ada Lovelace", "Some Consortium"]
    assert meta1["tags"] == ["ml", "biology"]
    assert meta1["abstract"] == "概要テキスト"
    # DOI recovered from a doi.org URL when the DOI field is empty
    assert rows["KEY2"]["doi"] == "10.1000/xyz123"

    assert db.get_sync_state("zotero")["cursor"] == {"library_version": 42}


def test_incremental_sends_since_and_advances_cursor(db):
    sync_zotero(client=transport(
        lambda p: httpx.Response(200, json=[zitem("KEY1")],
                                 headers={"Last-Modified-Version": "42"}), []))
    log = []
    changed = zitem("KEY1", title="改訂タイトル", modified="2026-07-01T00:00:00Z")
    stats = sync_zotero(client=transport(
        lambda p: httpx.Response(200, json=[changed],
                                 headers={"Last-Modified-Version": "50"}), log))

    assert log[0]["since"] == "42"
    assert stats["updated"] == 1
    assert db.get_sync_state("zotero")["cursor"] == {"library_version": 50}
    row = db.connect().execute(
        "SELECT title FROM items WHERE source='zotero' AND external_id='KEY1'"
    ).fetchone()
    assert row["title"] == "改訂タイトル"


def test_pagination_uses_start(db, monkeypatch):
    from app.connectors import zotero
    monkeypatch.setattr(zotero, "PAGE_LIMIT", 2)
    batches = {
        "0": [zitem("A"), zitem("B")],
        "2": [zitem("C")],
    }
    log = []
    stats = sync_zotero(client=transport(
        lambda p: httpx.Response(200, json=batches[p["start"]],
                                 headers={"Last-Modified-Version": "42"}), log))
    assert [p["start"] for p in log] == ["0", "2"]
    assert all(p["limit"] == "2" for p in log)
    assert stats["inserted"] == 3


def test_attachments_and_notes_filtered(db):
    items = [
        zitem("REAL"),
        zitem("ATT", item_type="attachment"),
        zitem("NOTE", item_type="note"),
    ]
    stats = sync_zotero(client=transport(
        lambda p: httpx.Response(200, json=items,
                                 headers={"Last-Modified-Version": "42"}), []))
    assert stats["fetched"] == 1
    assert db.connect().execute(
        "SELECT COUNT(*) FROM items WHERE source='zotero'").fetchone()[0] == 1


def test_full_sweep_prunes_deleted_references(db):
    """レビュー指摘 3.4: 完全な listing（--full）が成功したときだけ、Zotero 側で
    削除された文献を registry から prune する。増分では prune しない。"""
    sync_zotero(client=transport(
        lambda p: httpx.Response(200, json=[zitem("KEY1"), zitem("KEY2")],
                                 headers={"Last-Modified-Version": "42"}), []))
    # incremental: KEY2 was deleted upstream but must survive (no prune)
    stats = sync_zotero(client=transport(
        lambda p: httpx.Response(200, json=[],
                                 headers={"Last-Modified-Version": "43"}), []))
    assert stats["pruned"] == 0
    assert db.connect().execute(
        "SELECT COUNT(*) FROM items WHERE source='zotero'").fetchone()[0] == 2
    # full sweep: complete listing without KEY2 → pruned
    stats = sync_zotero(client=transport(
        lambda p: httpx.Response(200, json=[zitem("KEY1")],
                                 headers={"Last-Modified-Version": "43"}), []),
        full=True)
    assert stats["pruned"] == 1
    ids = {r["external_id"] for r in db.connect().execute(
        "SELECT external_id FROM items WHERE source='zotero'")}
    assert ids == {"KEY1"}


def test_empty_full_listing_does_not_prune(db):
    sync_zotero(client=transport(
        lambda p: httpx.Response(200, json=[zitem("KEY1")],
                                 headers={"Last-Modified-Version": "42"}), []))
    stats = sync_zotero(client=transport(
        lambda p: httpx.Response(200, json=[],
                                 headers={"Last-Modified-Version": "42"}), []),
        full=True)
    assert stats["pruned"] == 0
    assert db.connect().execute(
        "SELECT COUNT(*) FROM items WHERE source='zotero'").fetchone()[0] == 1


def test_error_keeps_cursor_and_records_last_error(db):
    sync_zotero(client=transport(
        lambda p: httpx.Response(200, json=[zitem("KEY1")],
                                 headers={"Last-Modified-Version": "42"}), []))
    with pytest.raises(httpx.HTTPStatusError):
        sync_zotero(client=httpx.Client(transport=httpx.MockTransport(
            lambda r: httpx.Response(403, text="denied"))))
    state = db.get_sync_state("zotero")
    assert "403" in state["last_error"]
    assert state["cursor"] == {"library_version": 42}


def test_missing_user_id_raises(db, monkeypatch):
    monkeypatch.delenv("CAIRN_ZOTERO_USER_ID")
    from app.connectors import ConnectorError
    with pytest.raises(ConnectorError):
        sync_zotero(client=httpx.Client(transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json=[]))))
