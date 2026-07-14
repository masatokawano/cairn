"""Integration tests for the ADR-0006 social import path.

Covers the glue the parser unit tests can't: parse → upsert_items (redaction
choke point) → item_text chunking → keyword search → item_links dedup with an
existing Karakeep bookmark → idempotent re-import. All fixtures are synthetic
(no real archive content — 不変条件の精神, ADR-0006 validation plan).
"""
import importlib
import json

import pytest


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("CAIRN_DB", str(tmp_path / "test.db"))
    from app import db as db_module
    importlib.reload(db_module)
    yield db_module
    conn = getattr(db_module._local, "conn", None)
    if conn:
        conn.close()
        db_module._local.conn = None


def _make_x_dir(tmp_path):
    d = tmp_path / "xarch" / "data"
    d.mkdir(parents=True, exist_ok=True)
    tweets = [{"tweet": {
        "id_str": "111",
        "full_text": "Cairnの横断検索設計についてポストした",
        "created_at": "Wed Oct 10 20:19:24 +0000 2018",
        "entities": {"urls": [{"expanded_url": "https://example.com/article"}]},
    }}]
    (d / "tweets.js").write_text(
        "window.YTD.tweets.part0 = " + json.dumps(tweets, ensure_ascii=False),
        encoding="utf-8",
    )
    likes = [{"like": {
        "tweetId": "222",
        "fullText": "とても面白い記事だった",
        "expandedUrl": "https://example.com/liked",
    }}]
    (d / "like.js").write_text(
        "window.YTD.like.part0 = " + json.dumps(likes, ensure_ascii=False),
        encoding="utf-8",
    )
    return tmp_path / "xarch"


def _make_fb_dir(tmp_path):
    root = tmp_path / "fbarch"
    posts_dir = root / "your_facebook_activity" / "posts"
    cr_dir = root / "your_facebook_activity" / "comments_and_reactions"
    posts_dir.mkdir(parents=True, exist_ok=True)
    cr_dir.mkdir(parents=True, exist_ok=True)

    def moji(s: str) -> str:  # DYI mojibake: UTF-8 bytes read as latin-1
        return s.encode("utf-8").decode("latin-1")

    posts = [{
        "timestamp": 1700000000,
        "data": [{"post": moji("外部脳の運用について投稿した")}],
        "attachments": [{"data": [{"external_context": {
            "url": "https://example.com/article"}}]}],
        "title": moji("テスト 太郎さんが投稿しました。"),
    }]
    (posts_dir / "your_posts__check_ins__photos_and_videos_1.json").write_text(
        json.dumps(posts, ensure_ascii=False), encoding="utf-8")
    comments = {"comments_v2": [{
        "timestamp": 1700000100,
        "data": [{"comment": {
            "timestamp": 1700000100,
            "comment": moji("これは自作コメントの本文です"),
            "author": moji("テスト 太郎"),
        }}],
        "title": moji("テスト 太郎さんが友人さんの投稿にコメントしました。"),
    }]}
    (cr_dir / "comments.json").write_text(
        json.dumps(comments, ensure_ascii=False), encoding="utf-8")
    return root


def test_import_x_end_to_end(db, tmp_path):
    from app.cli import _ingest_social
    from app.parsers.x_archive import parse_x_archive

    # Pre-existing Karakeep bookmark saving the same URL the like points at.
    db.upsert_items("karakeep", "bookmark", [{
        "external_id": "kb1", "title": "既存ブックマーク",
        "url": "https://example.com/liked", "meta": {},
    }])

    res = parse_x_archive(_make_x_dir(tmp_path))
    out = _ingest_social("x", [
        ("social_post", res.posts),
        ("bookmark", res.likes + res.bookmarks),
    ])
    assert out["kinds"]["social_post"]["inserted"] == 1
    assert out["kinds"]["bookmark"]["inserted"] == 1
    assert out["index"]["chunks"] >= 2  # both items got item_text chunks

    conn = db.connect()
    kinds = {r["kind"] for r in conn.execute(
        "SELECT kind FROM items WHERE source='x'")}
    assert kinds == {"social_post", "bookmark"}

    # Own post is keyword-searchable and typed social_post.
    hits = db.search("横断検索設計", kinds=["social_post"])
    assert hits and hits[0]["kind"] == "social_post"
    # The like's body is searchable as a bookmark, not a social_post.
    assert db.search("面白い記事", kinds=["social_post"]) == []
    assert db.search("面白い記事", kinds=["bookmark"])

    # urlnorm dedup: the X like and the Karakeep bookmark link via 'url'.
    linked = conn.execute("""
        SELECT COUNT(*) FROM item_links l
        JOIN items a ON a.id = l.a_id
        JOIN items b ON b.id = l.b_id
        WHERE l.link_via = 'url'
          AND ((a.source='karakeep' AND b.source='x')
            OR (a.source='x' AND b.source='karakeep'))
    """).fetchone()[0]
    assert linked == 1

    # Re-import is a no-op: everything skips, nothing re-indexes.
    res2 = parse_x_archive(_make_x_dir(tmp_path))
    out2 = _ingest_social("x", [
        ("social_post", res2.posts),
        ("bookmark", res2.likes + res2.bookmarks),
    ])
    assert out2["kinds"]["social_post"]["skipped"] == 1
    assert out2["kinds"]["bookmark"]["skipped"] == 1
    assert out2["index"] is None


def test_import_facebook_end_to_end(db, tmp_path):
    from app.cli import _ingest_social
    from app.parsers.facebook_dyi import parse_facebook_dyi

    res = parse_facebook_dyi(_make_fb_dir(tmp_path))
    out = _ingest_social("facebook", [
        ("social_post", res.posts + res.comments),
    ])
    assert out["kinds"]["social_post"]["inserted"] == 2

    conn = db.connect()
    rows = conn.execute(
        "SELECT title, meta FROM items WHERE source='facebook' ORDER BY id"
    ).fetchall()
    assert len(rows) == 2
    # Mojibake was repaired before storage (post + comment).
    assert rows[0]["title"] == "外部脳の運用について投稿した"
    comment_meta = json.loads(rows[1]["meta"])
    assert comment_meta["post_type"] == "comment"
    # 宛先文脈 (whose post the comment was on) is preserved, decoded.
    assert "友人さんの投稿にコメント" in comment_meta["reply_to_context"]

    # Both are searchable as social_post.
    assert db.search("外部脳の運用", kinds=["social_post"])
    assert db.search("自作コメントの本文", kinds=["social_post"])

    # Idempotent re-import.
    res2 = parse_facebook_dyi(_make_fb_dir(tmp_path))
    out2 = _ingest_social("facebook", [
        ("social_post", res2.posts + res2.comments),
    ])
    assert out2["kinds"]["social_post"]["skipped"] == 2
    assert out2["index"] is None
