from __future__ import annotations

from datetime import datetime, timezone

import pytest

from brainsync.connectors import cairn_api

NOW = datetime(2026, 7, 2, 12, 0, 0, tzinfo=timezone.utc)


def test_select_recent_reviews_filters_and_sorts(load_fixture):
    conversations = load_fixture("cairn_conversations.json")["results"]
    recent = cairn_api.select_recent_reviews(conversations, now=NOW)

    # 除外: New chat(2) / セキュリティレビュー定型(3) / message_count<4(4) /
    #       7日より古い(5) / タイトル空(6)。残りは新しい順。
    assert [item["id"] for item in recent] == [7, 1]


def test_is_review_candidate_edge_cases():
    base = {"title": "ok", "message_count": 4}
    assert cairn_api.is_review_candidate(base)
    assert not cairn_api.is_review_candidate({**base, "title": "  "})
    assert not cairn_api.is_review_candidate({**base, "title": "Untitled"})
    assert not cairn_api.is_review_candidate({**base, "message_count": 3})
    assert not cairn_api.is_review_candidate(
        {**base, "title": "You are a security expert reviewing this PR"}
    )


def test_fetch_conversations_builds_url(load_fixture):
    payload = load_fixture("cairn_conversations.json")
    calls = []

    def _get(url, headers=None, timeout=30):
        calls.append(url)
        return payload, {}

    results = cairn_api.fetch_conversations(
        "http://127.0.0.1:8730/", limit=500, get=_get
    )
    assert calls == ["http://127.0.0.1:8730/api/conversations?limit=500"]
    assert len(results) == 7


def test_check_accepts_contract_shape(load_fixture):
    payload = load_fixture("cairn_conversations.json")

    def _get(url, headers=None, timeout=30):
        return payload, {}

    results = cairn_api.check("http://127.0.0.1:8730", get=_get)
    assert len(results) == 7


def test_check_rejects_bad_shape():
    def _get(url, headers=None, timeout=30):
        return {"results": [{"id": "not-a-number", "source": "x"}]}, {}

    with pytest.raises(cairn_api.CheckError):
        cairn_api.check("http://127.0.0.1:8730", get=_get)
