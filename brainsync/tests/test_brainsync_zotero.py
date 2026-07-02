from __future__ import annotations

from datetime import datetime, timezone

import pytest

from brainsync.connectors import zotero

NOW = datetime(2026, 7, 2, 12, 0, 0, tzinfo=timezone.utc)


def test_fetch_sends_headers_and_returns_version(load_fixture):
    items_fixture = load_fixture("zotero_items.json")
    seen = {}

    def _get(url, headers=None, timeout=30):
        seen["url"] = url
        seen.update(headers or {})
        return items_fixture, {"Last-Modified-Version": "12345"}

    items, version = zotero.fetch_recent_items(
        "https://api.zotero.org", "9999", "sekrit", get=_get
    )
    assert seen["Zotero-API-Key"] == "sekrit"
    assert seen["Zotero-API-Version"] == "3"
    assert "/users/9999/items/top?" in seen["url"]
    assert version == "12345"
    assert len(items) == 3


def test_select_recent_filters_and_sorts(load_fixture):
    items = load_fixture("zotero_items.json")
    recent = zotero.select_recent(items, now=NOW)
    assert [item["key"] for item in recent] == ["KEY1", "KEY2"]


def test_creator_name_variants():
    assert zotero.creator_name({"name": "Google Brain"}) == "Google Brain"
    assert (
        zotero.creator_name({"firstName": "Ashish", "lastName": "Vaswani"})
        == "Ashish Vaswani"
    )
    assert zotero.creator_name({"lastName": "Vaswani"}) == "Vaswani"
    assert zotero.creator_name({}) == ""


def test_check_rejects_bad_shape():
    def _get(url, headers=None, timeout=30):
        return [{"key": 123, "version": "x", "data": []}], {}

    with pytest.raises(zotero.CheckError):
        zotero.check("https://api.zotero.org", "9999", "sekrit", get=_get)
