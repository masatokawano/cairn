"""Tests for the X (Twitter) archive parser.

All fixture content is synthetic (fake ids, fake handles, no real people).
Archives are built both as an extracted directory and as a ZIP so we can
prove directory/ZIP parity.
"""
import json
import zipfile

from app.parsers.x_archive import parse_x_archive

# --- synthetic archive content -------------------------------------------------

TWEETS = [
    {"tweet": {
        "id_str": "111",
        "full_text": "Hello world from a synthetic post",
        "created_at": "Wed Oct 10 20:19:24 +0000 2018",
        "entities": {"urls": [{"expanded_url": "https://example.com/a"}]},
    }},
    {"tweet": {
        "id_str": "222",
        "full_text": "@someone this is my synthetic reply",
        "created_at": "Thu Oct 11 08:00:00 +0000 2018",
        "in_reply_to_status_id_str": "111",
        "entities": {"urls": []},
    }},
]

LIKES = [
    {"like": {"tweetId": "333", "fullText": "a liked tweet body",
              "expandedUrl": "https://example.net/liked"}},
    {"like": {"tweetId": "444"}},  # no fullText, no expandedUrl
]

BOOKMARKS = [
    {"bookmark": {"tweetId": "555", "fullText": "a bookmarked tweet",
                  "expandedUrl": "https://example.org/bm"}},
]


def _js(var, arr):
    """Reproduce X's `window.YTD.<var> = [ ... ]` assignment wrapper."""
    return f"window.YTD.{var} = " + json.dumps(arr)


def _write_dir(base, *, tweets=TWEETS, likes=LIKES, bookmarks=None,
               tweets_name="tweets.js", extra_parts=None):
    data = base / "data"
    data.mkdir(parents=True)
    (data / tweets_name).write_text(_js("tweets.part0", tweets), encoding="utf-8")
    (data / "like.js").write_text(_js("like.part0", likes), encoding="utf-8")
    if bookmarks is not None:
        (data / "bookmark.js").write_text(_js("bookmark.part0", bookmarks), encoding="utf-8")
    for fname, arr in (extra_parts or {}).items():
        (data / fname).write_text(_js("tweets.part1", arr), encoding="utf-8")
    return base


def _write_zip(zip_path, *, tweets=TWEETS, likes=LIKES, bookmarks=None):
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("data/tweets.js", _js("tweets.part0", tweets))
        zf.writestr("data/like.js", _js("like.part0", likes))
        if bookmarks is not None:
            zf.writestr("data/bookmark.js", _js("bookmark.part0", bookmarks))
    return zip_path


# --- tests ---------------------------------------------------------------------

def test_wrapper_stripping_and_post_fields(tmp_path):
    result = parse_x_archive(_write_dir(tmp_path / "arc"))
    by_id = {p["external_id"]: p for p in result.posts}

    post = by_id["x:111"]
    # url feeds items.url_norm (the only thing rebuild_item_links() reads),
    # so a post with an embedded link uses that link — enabling Karakeep
    # dedup — not the tweet's own permalink (2026-07-16: found via production
    # import producing zero X item_links; see test_post_without_link_falls_
    # back_to_permalink for the no-link case).
    assert post["url"] == "https://example.com/a"
    assert post["title"] == "Hello world from a synthetic post"
    assert post["meta"]["text"] == "Hello world from a synthetic post"
    assert post["meta"]["social_source"] == "x"
    assert post["meta"]["post_type"] == "post"
    assert post["meta"]["links"] == ["https://example.com/a"]
    assert "reply_to_url" not in post["meta"]
    # created_at "Wed Oct 10 20:19:24 +0000 2018" -> ISO-8601 UTC
    assert post["created_at"] == "2018-10-10T20:19:24+00:00"
    assert post["updated_at"] == post["created_at"]


def test_reply_detection(tmp_path):
    result = parse_x_archive(_write_dir(tmp_path / "arc"))
    reply = {p["external_id"]: p for p in result.posts}["x:222"]
    assert reply["meta"]["post_type"] == "reply"
    assert reply["meta"]["reply_to_url"] == "https://x.com/i/status/111"
    assert reply["meta"]["links"] == []
    assert result.counts["tweets_seen"] == 2


def test_post_without_link_falls_back_to_permalink(tmp_path):
    """No embedded link (meta["links"] == []) -> url stays the tweet's own
    permalink, so plain-text posts remain clickable in the UI."""
    result = parse_x_archive(_write_dir(tmp_path / "arc"))
    reply = {p["external_id"]: p for p in result.posts}["x:222"]
    assert reply["url"] == "https://x.com/i/status/222"


def test_older_tweet_js_name_supported(tmp_path):
    result = parse_x_archive(_write_dir(tmp_path / "arc", tweets_name="tweet.js"))
    assert {p["external_id"] for p in result.posts} == {"x:111", "x:222"}


def test_multipart_tweets_concatenated(tmp_path):
    extra = [{"tweet": {"id_str": "999", "full_text": "part-1 tweet",
                        "created_at": "Fri Oct 12 09:00:00 +0000 2018"}}]
    result = parse_x_archive(
        _write_dir(tmp_path / "arc", extra_parts={"tweets-part1.js": extra})
    )
    assert {p["external_id"] for p in result.posts} == {"x:111", "x:222", "x:999"}


def test_like_without_full_text_kept(tmp_path):
    result = parse_x_archive(_write_dir(tmp_path / "arc"))
    by_id = {lk["external_id"]: lk for lk in result.likes}

    with_text = by_id["x-like:333"]
    assert with_text["meta"]["action"] == "like"
    assert with_text["meta"]["text"] == "a liked tweet body"
    assert with_text["url"] == "https://example.net/liked"
    assert with_text["created_at"] is None

    # No fullText: still ingested (carries the URL), but text omitted + counted.
    without_text = by_id["x-like:444"]
    assert "text" not in without_text["meta"]
    assert without_text["url"] == "https://x.com/i/status/444"
    assert result.counts["likes_seen"] == 2
    assert result.counts["skipped_no_text"] == 1


def test_missing_bookmarks_file_yields_empty_list(tmp_path):
    result = parse_x_archive(_write_dir(tmp_path / "arc"))  # no bookmarks passed
    assert result.bookmarks == []
    assert result.counts["bookmarks_seen"] == 0


def test_bookmarks_file_present_parsed(tmp_path):
    result = parse_x_archive(_write_dir(tmp_path / "arc", bookmarks=BOOKMARKS))
    assert len(result.bookmarks) == 1
    bm = result.bookmarks[0]
    assert bm["external_id"] == "x-bookmark:555"
    assert bm["meta"]["action"] == "bookmark"
    assert bm["meta"]["text"] == "a bookmarked tweet"
    assert bm["url"] == "https://example.org/bm"
    assert result.counts["bookmarks_seen"] == 1


def test_title_truncated_to_80(tmp_path):
    long_first = "x" * 200
    tweets = [{"tweet": {"id_str": "1", "full_text": long_first + "\nsecond line",
                         "created_at": "Wed Oct 10 20:19:24 +0000 2018"}}]
    result = parse_x_archive(_write_dir(tmp_path / "arc", tweets=tweets))
    assert result.posts[0]["title"] == "x" * 80


def test_zip_and_dir_parity(tmp_path):
    dir_result = parse_x_archive(_write_dir(tmp_path / "arc", bookmarks=BOOKMARKS))
    zip_result = parse_x_archive(_write_zip(tmp_path / "arc.zip", bookmarks=BOOKMARKS))
    assert zip_result.posts == dir_result.posts
    assert zip_result.likes == dir_result.likes
    assert zip_result.bookmarks == dir_result.bookmarks
    assert zip_result.counts == dir_result.counts


def test_external_ids_deterministic_across_reparse(tmp_path):
    base = _write_dir(tmp_path / "arc", bookmarks=BOOKMARKS)
    first = parse_x_archive(base)
    second = parse_x_archive(base)
    assert [p["external_id"] for p in first.posts] == [p["external_id"] for p in second.posts]
    assert [b["external_id"] for b in first.bookmarks] == [b["external_id"] for b in second.bookmarks]


def test_textless_tweet_skipped(tmp_path):
    """A tweet with no body text is skipped entirely (Codex review should #2:
    symmetric with facebook_dyi's photo-only posts) — unlike likes/bookmarks,
    which are kept for their URL alone."""
    tweets = [
        {"tweet": {"id_str": "666", "full_text": "   ",
                   "created_at": "Wed Oct 10 20:19:24 +0000 2018"}},
        {"tweet": {"id_str": "777", "full_text": "has text",
                   "created_at": "Wed Oct 10 20:19:24 +0000 2018"}},
    ]
    result = parse_x_archive(_write_dir(tmp_path / "arc", tweets=tweets, likes=[]))
    assert [p["external_id"] for p in result.posts] == ["x:777"]
    assert result.counts["tweets_seen"] == 2
    assert result.counts["skipped_no_text"] == 1
