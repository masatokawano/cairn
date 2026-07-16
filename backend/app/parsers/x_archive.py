"""Parser for the X (Twitter) official "your archive" export.

Slot: this is a *parser* (static export), not a live connector — matching
the posture in ADR-0006. It reads three kinds of self-authored / self-curated
activity and nothing else (no feed, no follower graph, no DMs):

  - own posts and replies      -> `posts`     (become items.kind='social_post')
  - likes (with body text)     -> `likes`     (become items.kind='bookmark', action='like')
  - bookmarks (with body text) -> `bookmarks` (become items.kind='bookmark', action='bookmark')

Archive layout (a ZIP, or an already-extracted directory):

  data/tweets.js   — own tweets + replies. Older exports name it `tweet.js`;
                     large accounts split it into `tweets.js` (part 0) plus
                     `tweets-part1.js`, `tweets-part2.js`, … — all matched
                     parts are concatenated.
  data/like.js     — liked tweets, each carrying `fullText` and `expandedUrl`.
  data/bookmark.js — bookmarked tweets (OPTIONAL: X does not always include
                     bookmarks in the archive; when absent, `bookmarks` is []).

Every `.js` file is a JavaScript assignment wrapper, e.g.
`window.YTD.tweets.part0 = [ {...}, {...} ]`. We strip everything up to the
first `=` and `json.loads` the remaining JSON array.

Untrusted input (不変条件 4 / ADR-0006 §4): archive text is adversarial data.
It is parsed as JSON only — never evaluated — and every entry is best-effort:
a malformed file or record is skipped rather than aborting the whole import.
"""
from __future__ import annotations

import json
import os
import re
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

SOURCE = "x"

# `tweets.js` / `tweet.js` / `tweets-part1.js` — but NOT `tweet_headers.js`.
_TWEETS_RE = re.compile(r"(^|/)tweets?(-part\d+)?\.js$")
_LIKES_RE = re.compile(r"(^|/)likes?(-part\d+)?\.js$")
_BOOKMARKS_RE = re.compile(r"(^|/)bookmarks?(-part\d+)?\.js$")

# X's created_at format, e.g. "Wed Oct 10 20:19:24 +0000 2018".
_X_TIME_FMT = "%a %b %d %H:%M:%S %z %Y"


@dataclass
class XArchiveResult:
    posts: list[dict] = field(default_factory=list)
    likes: list[dict] = field(default_factory=list)
    bookmarks: list[dict] = field(default_factory=list)
    counts: dict = field(default_factory=dict)


def _title(text: str, limit: int = 80) -> str:
    """First non-empty line of `text`, truncated to `limit` chars."""
    stripped = text.strip()
    if not stripped:
        return ""
    return stripped.splitlines()[0][:limit]


def _iso_utc(created_at: str | None) -> str | None:
    if not created_at:
        return None
    try:
        dt = datetime.strptime(created_at, _X_TIME_FMT)
    except (ValueError, TypeError):
        return None
    return dt.astimezone(timezone.utc).isoformat()


@contextmanager
def _archive(path: str | Path):
    """Yield ``(names, read)`` for a ZIP file or an extracted directory.

    `names` lists every member's path (POSIX-style, deterministic order);
    `read(name)` returns that member's raw bytes. Only members we actually
    match are ever read, so unrelated files are never opened.
    """
    path = Path(path)
    if path.is_dir():
        names: list[str] = []
        for root, _dirs, files in os.walk(path):
            for f in files:
                rel = str((Path(root) / f).relative_to(path)).replace(os.sep, "/")
                names.append(rel)
        names.sort()

        def read(name: str) -> bytes:
            return (path / name).read_bytes()

        yield names, read
    elif zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as zf:
            names = sorted(
                n for n in zf.namelist()
                if not n.endswith("/") and not n.startswith("__MACOSX")
            )
            yield names, zf.read
    else:
        raise ValueError(f"X アーカイブが ZIP でもディレクトリでもありません: {path}")


def _load_js_array(raw: bytes) -> list:
    """Strip the `window.YTD.* = ` wrapper and parse the JSON array body.

    Returns [] for anything that doesn't decode to a list, so a single
    corrupt file degrades to "no records" rather than raising.
    """
    try:
        text = raw.decode("utf-8", "replace")
    except (UnicodeDecodeError, AttributeError):
        return []
    eq = text.find("=")
    if eq == -1:
        return []
    try:
        data = json.loads(text[eq + 1:])
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


def _post_record(tweet: dict, counts: dict) -> dict | None:
    tweet_id = tweet.get("id_str") or tweet.get("id")
    if not tweet_id:
        return None
    counts["tweets_seen"] += 1

    full_text = tweet.get("full_text")
    if not isinstance(full_text, str):
        full_text = tweet.get("text") if isinstance(tweet.get("text"), str) else ""

    reply_to = tweet.get("in_reply_to_status_id_str") or tweet.get("in_reply_to_status_id")
    links = [
        u["expanded_url"]
        for u in ((tweet.get("entities") or {}).get("urls") or [])
        if isinstance(u, dict) and isinstance(u.get("expanded_url"), str) and u["expanded_url"]
    ]

    # Textless own tweets are skipped, matching facebook_dyi's photo-only
    # posts (Codex review 2026-07-14, should #2): a social_post with nothing
    # to search on is a noise row. (Likes/bookmarks differ deliberately —
    # their URL alone is the curation signal, see _curated_record.)
    if not full_text.strip():
        counts["skipped_no_text"] += 1
        return None

    meta: dict = {
        "social_source": SOURCE,
        "post_type": "reply" if reply_to else "post",
        "links": links,
        "text": full_text,
    }
    if reply_to:
        meta["reply_to_url"] = f"https://x.com/i/status/{reply_to}"

    iso = _iso_utc(tweet.get("created_at"))
    # url feeds items.url_norm (db.upsert_items) which is the only thing
    # rebuild_item_links() reads — meta["links"] alone is invisible to the
    # linker. Prefer the tweet's first embedded external link so a post
    # sharing an article dedups against a Karakeep copy of it (matching
    # facebook_dyi._post_record's external_context.url pattern); fall back
    # to the tweet's own permalink so plain-text posts stay clickable.
    return {
        "external_id": f"x:{tweet_id}",
        "title": _title(full_text),
        "url": links[0] if links else f"https://x.com/i/status/{tweet_id}",
        "created_at": iso,
        "updated_at": iso,
        "meta": meta,
    }


def _curated_record(obj: dict, *, action: str, id_prefix: str, counts: dict,
                    seen_key: str) -> dict | None:
    """Build a like/bookmark record. `obj` is the inner object (the value of
    the `like` / `bookmark` wrapper key). Kept even without body text — the
    URL alone is still worth indexing — but counted in `skipped_no_text`.
    """
    tweet_id = obj.get("tweetId") or obj.get("tweet_id") or obj.get("id")
    if not tweet_id:
        return None
    counts[seen_key] += 1

    full_text = obj.get("fullText") or obj.get("full_text")
    if not isinstance(full_text, str):
        full_text = ""
    expanded = obj.get("expandedUrl") or obj.get("expanded_url")

    meta: dict = {"social_source": SOURCE, "action": action}
    if full_text.strip():
        meta["text"] = full_text
    else:
        counts["skipped_no_text"] += 1

    return {
        "external_id": f"{id_prefix}{tweet_id}",
        "title": _title(full_text),
        "url": expanded if isinstance(expanded, str) and expanded
        else f"https://x.com/i/status/{tweet_id}",
        "created_at": None,  # like.js / bookmark.js carry no timestamp
        "updated_at": None,
        "meta": meta,
    }


def parse_x_archive(path: str | Path) -> XArchiveResult:
    """Parse an X archive (ZIP path or extracted directory) into an
    ``XArchiveResult``. See module docstring for the record shapes."""
    counts = {
        "tweets_seen": 0,
        "likes_seen": 0,
        "bookmarks_seen": 0,
        "skipped_no_text": 0,
    }
    posts: list[dict] = []
    likes: list[dict] = []
    bookmarks: list[dict] = []

    with _archive(path) as (names, read):
        for name in names:
            if _TWEETS_RE.search(name):
                for entry in _load_js_array(read(name)):
                    if not isinstance(entry, dict):
                        continue
                    tweet = entry.get("tweet") if isinstance(entry.get("tweet"), dict) else entry
                    rec = _post_record(tweet, counts)
                    if rec is not None:
                        posts.append(rec)
            elif _LIKES_RE.search(name):
                for entry in _load_js_array(read(name)):
                    if not isinstance(entry, dict):
                        continue
                    inner = entry.get("like") if isinstance(entry.get("like"), dict) else entry
                    rec = _curated_record(
                        inner, action="like", id_prefix="x-like:",
                        counts=counts, seen_key="likes_seen",
                    )
                    if rec is not None:
                        likes.append(rec)
            elif _BOOKMARKS_RE.search(name):
                for entry in _load_js_array(read(name)):
                    if not isinstance(entry, dict):
                        continue
                    inner = entry.get("bookmark") if isinstance(entry.get("bookmark"), dict) else entry
                    rec = _curated_record(
                        inner, action="bookmark", id_prefix="x-bookmark:",
                        counts=counts, seen_key="bookmarks_seen",
                    )
                    if rec is not None:
                        bookmarks.append(rec)

    return XArchiveResult(posts=posts, likes=likes, bookmarks=bookmarks, counts=counts)
