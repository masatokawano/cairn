from __future__ import annotations

import urllib.parse

from brainsync.connectors import karakeep


def make_get(pages: list[dict]):
    """呼ばれるたびに pages を順に返す偽 get_json。呼び出し URL を記録する。"""
    calls: list[str] = []

    def _get(url, headers=None, timeout=30):
        calls.append(url)
        return pages[len(calls) - 1], {}

    return _get, calls


def query_params(url: str) -> dict[str, list[str]]:
    return urllib.parse.parse_qs(urllib.parse.urlparse(url).query)


def test_pagination_follows_next_cursor(load_fixture):
    pages = [load_fixture("karakeep_page1.json"), load_fixture("karakeep_page2.json")]
    get, calls = make_get(pages)

    bookmarks = karakeep.fetch_bookmarks("https://keep.example.com/", "key", get=get)

    assert len(calls) == 2
    assert "cursor" not in query_params(calls[0])
    assert query_params(calls[1])["cursor"] == ["cursor-2"]
    assert [b["id"] for b in bookmarks] == [
        "bk_001",
        "bk_002",
        "bk_003",
        "bk_101",
        "bk_102",
    ]


def test_pagination_respects_max_pages(load_fixture):
    # 常に nextCursor を返し続けるページで打ち切りを確認
    page = load_fixture("karakeep_page1.json")
    get, calls = make_get([page, page, page, page])

    bookmarks = karakeep.fetch_bookmarks(
        "https://keep.example.com", "key", max_pages=3, get=get
    )

    assert len(calls) == 3
    assert len(bookmarks) == 9


def test_auth_header_is_sent(load_fixture):
    seen = {}

    def _get(url, headers=None, timeout=30):
        seen.update(headers or {})
        return load_fixture("karakeep_page2.json"), {}

    karakeep.fetch_bookmarks("https://keep.example.com", "sekrit", get=_get)
    assert seen["Authorization"] == "Bearer sekrit"


def test_select_to_review(load_fixture):
    bookmarks = (
        load_fixture("karakeep_page1.json")["bookmarks"]
        + load_fixture("karakeep_page2.json")["bookmarks"]
    )
    selected = karakeep.select_to_review(bookmarks)
    assert [b["id"] for b in selected] == ["bk_001", "bk_002", "bk_101"]


def test_count_untagged(load_fixture):
    bookmarks = (
        load_fixture("karakeep_page1.json")["bookmarks"]
        + load_fixture("karakeep_page2.json")["bookmarks"]
    )
    assert karakeep.count_untagged(bookmarks) == 1


def test_title_fallback_chain(load_fixture):
    bookmarks = {
        b["id"]: b
        for b in (
            load_fixture("karakeep_page1.json")["bookmarks"]
            + load_fixture("karakeep_page2.json")["bookmarks"]
        )
    }
    # 自身の title → content.title → content.url → 無題
    assert karakeep.bookmark_title(bookmarks["bk_001"]) == "LLM agents survey"
    assert karakeep.bookmark_title(bookmarks["bk_002"]) == "Content title wins"
    assert (
        karakeep.bookmark_title(bookmarks["bk_101"])
        == "https://example.com/older-to-review"
    )
    assert karakeep.bookmark_title({"content": {}}) == "無題"
