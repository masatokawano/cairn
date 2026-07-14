"""Tests for the Facebook DYI parser.

All fixture content is synthetic. Japanese strings are written as *mojibake*
(UTF-8 bytes reinterpreted through latin-1) exactly as a real DYI export
stores them, and the parser is asserted to recover the true text. Fixtures
also include decoy out-of-scope files (likes/reactions, direct messages) to
prove the parser never reads them.
"""
import json
import zipfile

from app.parsers.facebook_dyi import parse_facebook_dyi

POSTS_REL = ("your_facebook_activity/posts/"
             "your_posts__check_ins__photos_and_videos_1.json")
COMMENTS_REL = "your_facebook_activity/comments_and_reactions/comments.json"
DECOY_LIKES_REL = ("your_facebook_activity/likes_and_reactions/"
                   "likes_and_reactions_1.json")
DECOY_DM_REL = "your_facebook_activity/messages/inbox/direct_messages.json"


def _moji(true_text):
    """Encode a clean string the way DYI stores it: UTF-8 bytes seen as latin-1."""
    return true_text.encode("utf-8").decode("latin-1")


POSTS = [
    {
        "timestamp": 1600000000,
        "title": _moji("Test Userさんが近況を投稿しました"),
        "data": [{"post": _moji("これは合成テスト投稿です")}],
        "attachments": [{"data": [{"external_context": {"url": "https://example.com/fb"}}]}],
    },
    {  # photo-only: no post text -> must be skipped
        "timestamp": 1600000100,
        "title": _moji("写真"),
        "data": [{"media": {"uri": "media/x.jpg"}}],
        "attachments": [],
    },
]

COMMENTS = {"comments_v2": [
    {
        "timestamp": 1600001000,
        "title": _moji("Alice Testさんの投稿にコメントしました"),
        "data": [{"comment": {
            "timestamp": 1600001000,
            "comment": _moji("とても良い投稿ですね"),
            "author": _moji("Test User"),
        }}],
    },
    {  # sticker/photo reaction with no comment text -> skipped
        "timestamp": 1600001100,
        "title": _moji("スタンプ"),
        "data": [{"comment": {"timestamp": 1600001100, "author": _moji("Test User")}}],
    },
]}

# Sentinel strings that must NEVER surface in the result.
DECOY_LIKES = [{"timestamp": 1600002000,
                "title": _moji("SENTINEL_LIKE_should_not_appear"),
                "data": [{"reaction": {"reaction": "LIKE"}}]}]
DECOY_DM = {"messages": [{"content": _moji("SENTINEL_DM_should_not_appear")}]}


def _write_dir(base):
    for rel, payload in (
        (POSTS_REL, POSTS),
        (COMMENTS_REL, COMMENTS),
        (DECOY_LIKES_REL, DECOY_LIKES),
        (DECOY_DM_REL, DECOY_DM),
    ):
        p = base / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return base


def _write_zip(zip_path):
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr(POSTS_REL, json.dumps(POSTS, ensure_ascii=False))
        zf.writestr(COMMENTS_REL, json.dumps(COMMENTS, ensure_ascii=False))
        zf.writestr(DECOY_LIKES_REL, json.dumps(DECOY_LIKES, ensure_ascii=False))
        zf.writestr(DECOY_DM_REL, json.dumps(DECOY_DM, ensure_ascii=False))
    return zip_path


# --- tests ---------------------------------------------------------------------

def test_post_text_and_links_decoded(tmp_path):
    result = parse_facebook_dyi(_write_dir(tmp_path / "dyi"))
    assert len(result.posts) == 1
    post = result.posts[0]
    assert post["meta"]["text"] == "これは合成テスト投稿です"  # mojibake repaired
    assert post["title"] == "これは合成テスト投稿です"
    assert post["meta"]["social_source"] == "facebook"
    assert post["meta"]["post_type"] == "post"
    assert post["url"] == "https://example.com/fb"
    assert post["meta"]["links"] == ["https://example.com/fb"]
    assert post["created_at"] == "2020-09-13T12:26:40+00:00"
    assert post["external_id"].startswith("fb:")


def test_photo_only_post_skipped(tmp_path):
    result = parse_facebook_dyi(_write_dir(tmp_path / "dyi"))
    assert result.counts["posts_seen"] == 2
    assert len(result.posts) == 1
    assert result.counts["skipped_no_text"] >= 1


def test_comment_context_preserved_and_decoded(tmp_path):
    result = parse_facebook_dyi(_write_dir(tmp_path / "dyi"))
    assert len(result.comments) == 1
    c = result.comments[0]
    assert c["meta"]["text"] == "とても良い投稿ですね"
    assert c["meta"]["post_type"] == "comment"
    # 宛先文脈: whose post this replied to — decoded and preserved.
    assert c["meta"]["reply_to_context"] == "Alice Testさんの投稿にコメントしました"
    assert c["meta"]["author"] == "Test User"
    assert c["url"] is None
    assert c["created_at"] == "2020-09-13T12:43:20+00:00"
    assert c["external_id"].startswith("fbcomment:")


def test_comment_without_text_skipped(tmp_path):
    result = parse_facebook_dyi(_write_dir(tmp_path / "dyi"))
    assert result.counts["comments_seen"] == 2
    assert len(result.comments) == 1


def test_decoy_files_are_never_reflected(tmp_path):
    result = parse_facebook_dyi(_write_dir(tmp_path / "dyi"))
    blob = json.dumps(
        {"posts": result.posts, "comments": result.comments, "counts": result.counts},
        ensure_ascii=False,
    )
    assert "SENTINEL_LIKE_should_not_appear" not in blob
    assert "SENTINEL_DM_should_not_appear" not in blob
    # only the two in-scope sources contributed
    assert result.counts["posts_seen"] == 2
    assert result.counts["comments_seen"] == 2


def test_external_ids_deterministic_across_reparse(tmp_path):
    base = _write_dir(tmp_path / "dyi")
    first = parse_facebook_dyi(base)
    second = parse_facebook_dyi(base)
    assert [p["external_id"] for p in first.posts] == [p["external_id"] for p in second.posts]
    assert [c["external_id"] for c in first.comments] == [c["external_id"] for c in second.comments]


def test_zip_and_dir_parity(tmp_path):
    dir_result = parse_facebook_dyi(_write_dir(tmp_path / "dyi"))
    zip_result = parse_facebook_dyi(_write_zip(tmp_path / "dyi.zip"))
    assert zip_result.posts == dir_result.posts
    assert zip_result.comments == dir_result.comments
    assert zip_result.counts == dir_result.counts


def _write_comments_only_dir(base, comments):
    p = base / COMMENTS_REL
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(comments, ensure_ascii=False), encoding="utf-8")
    return base


def _comment_entry(text, author):
    return {
        "timestamp": 1600003000,
        "title": _moji("Test Userさんが投稿にコメントしました"),
        "data": [{"comment": {
            "timestamp": 1600003000,
            "comment": _moji(text),
            "author": _moji(author),
        }}],
    }


def test_comment_authors_seen_counts_distinct_authors(tmp_path):
    """comment_authors_seen surfaces how many distinct authors appeared in
    comments.json — the self-authored-only format assumption is only safe
    while this stays at 1 (Codex review should #1). Only the count is
    exposed, never a name."""
    comments = {"comments_v2": [
        _comment_entry("first synthetic comment", "Test User"),
        _comment_entry("second synthetic comment", "Test User"),
    ]}
    result = parse_facebook_dyi(_write_comments_only_dir(tmp_path / "arc", comments))
    assert result.counts["comment_authors_seen"] == 1

    # A hypothetical future format mixing in another author is detectable
    # by count alone (no names in stats).
    comments2 = {"comments_v2": [
        _comment_entry("own synthetic comment", "Test User"),
        _comment_entry("someone else's synthetic comment", "Other Person"),
    ]}
    result2 = parse_facebook_dyi(_write_comments_only_dir(tmp_path / "arc2", comments2))
    assert result2.counts["comment_authors_seen"] == 2
    assert all(not isinstance(v, str) or "Other Person" not in v
               for v in result2.counts.values())  # stats carry counts, not names
