"""Parser for the Facebook "Download Your Information" (DYI) JSON export.

Scope (ADR-0006, owner-mandated 2026-07-14): Facebook ingest is limited to
self-authored content only — your own posts and the comments you wrote. It
reads EXACTLY two sources and opens nothing else in the archive (no
likes/reactions, no media, no friends, no DMs, no feed):

  your_facebook_activity/posts/your_posts__check_ins__photos_and_videos_*.json
      -> `posts`    (become items.kind='social_post')
  your_facebook_activity/comments_and_reactions/comments.json
      -> `comments` (become items.kind='social_post', post_type='comment')

Enforcement: only members matching those two path patterns are ever read, so
files such as `likes_and_reactions_*.json` or message archives are never
opened — see the decoy test.

Mojibake (required, verified against a real DYI): DYI encodes text as UTF-8
bytes reinterpreted through latin-1. Every extracted string is repaired with
``s.encode('latin-1').decode('utf-8')`` and falls back to the raw string on
any codec error (some strings are already clean, e.g. pure ASCII).

Comment 宛先文脈 (ADR-0006 open question 4): the export never contains other
people's post bodies, but each comment carries a top-level `title` such as
"◯◯さんの投稿にコメントしました" describing whose post it replied to. That is
the author's own action context, so it is preserved (decoded) as
`meta.reply_to_context`.

Untrusted input (不変条件 4): archive text is adversarial data — parsed as
JSON only, never evaluated; malformed records are skipped, not raised.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

SOURCE = "facebook"

_POSTS_RE = re.compile(
    r"(^|/)your_facebook_activity/posts/"
    r"your_posts__check_ins__photos_and_videos_[^/]*\.json$"
)
_COMMENTS_RE = re.compile(
    r"(^|/)your_facebook_activity/comments_and_reactions/comments\.json$"
)


@dataclass
class FacebookDyiResult:
    posts: list[dict] = field(default_factory=list)
    comments: list[dict] = field(default_factory=list)
    counts: dict = field(default_factory=dict)


def _demojibake(s: str) -> str:
    """Repair DYI's latin-1-wrapped UTF-8; return the input on codec errors."""
    if not isinstance(s, str):
        return s
    try:
        return s.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return s


def _title(text: str, limit: int = 80) -> str:
    stripped = text.strip()
    if not stripped:
        return ""
    return stripped.splitlines()[0][:limit]


def _iso_utc(unix_ts) -> str | None:
    try:
        return datetime.fromtimestamp(int(unix_ts), tz=timezone.utc).isoformat()
    except (ValueError, TypeError, OSError):
        return None


def _fb_id(prefix: str, timestamp, text: str) -> str:
    """Deterministic id across re-exports: same timestamp+text -> same id."""
    digest = hashlib.sha256(f"{timestamp}\n{text}".encode("utf-8")).hexdigest()
    return f"{prefix}{digest[:24]}"


@contextmanager
def _archive(path: str | Path):
    """Yield ``(names, read)`` for a ZIP file or an extracted directory.

    Only members we explicitly read are opened; listing names does not open
    anything, so out-of-scope files (likes/reactions, media, DMs) are never
    touched.
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
        raise ValueError(f"Facebook DYI が ZIP でもディレクトリでもありません: {path}")


def _load_json(raw: bytes):
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except (json.JSONDecodeError, AttributeError):
        return None


def _post_record(record: dict, counts: dict) -> dict | None:
    if not isinstance(record, dict):
        return None
    counts["posts_seen"] += 1

    texts = [
        _demojibake(d["post"])
        for d in (record.get("data") or [])
        if isinstance(d, dict) and isinstance(d.get("post"), str) and d["post"].strip()
    ]
    text = "\n".join(texts)
    if not text.strip():
        counts["skipped_no_text"] += 1  # photo-only / check-in-only record
        return None

    links = [
        _demojibake(ec["url"])
        for att in (record.get("attachments") or [])
        if isinstance(att, dict)
        for d in (att.get("data") or [])
        if isinstance(d, dict)
        for ec in [d.get("external_context") or {}]
        if isinstance(ec.get("url"), str) and ec["url"]
    ]

    timestamp = record.get("timestamp")
    iso = _iso_utc(timestamp)
    return {
        "external_id": _fb_id("fb:", timestamp, text),
        "title": _title(text),
        "url": links[0] if links else None,
        "created_at": iso,
        "updated_at": iso,
        "meta": {
            "text": text,
            "social_source": SOURCE,
            "post_type": "post",
            "links": links,
        },
    }


def _comment_records(entry: dict, counts: dict, authors: set[str]) -> list[dict]:
    """FORMAT ASSUMPTION (Codex review 2026-07-14, should #1): the
    "self-authored only" guarantee rests on Meta's DYI semantics —
    comments.json contains only comments the account owner wrote (verified
    against a real export: distinct author == 1). There is no owner-name
    input to filter against, so if a future DYI revision ever mixes in other
    people's comments they would be ingested as social_post. To make that
    detectable without logging any name, every distinct author feeds the
    `comment_authors_seen` count surfaced in import stats — a value > 1 on a
    future import is the signal to re-verify the format before trusting it.
    """
    if not isinstance(entry, dict):
        return []
    reply_to_context = _demojibake(entry.get("title") or "")
    out: list[dict] = []
    for d in (entry.get("data") or []):
        if not isinstance(d, dict):
            continue
        comment = d.get("comment")
        if not isinstance(comment, dict):
            continue
        counts["comments_seen"] += 1
        if isinstance(comment.get("author"), str) and comment["author"]:
            authors.add(comment["author"])
        raw_text = comment.get("comment")
        if not isinstance(raw_text, str) or not raw_text.strip():
            counts["skipped_no_text"] += 1  # sticker/photo-only reaction
            continue
        text = _demojibake(raw_text)
        # comment.timestamp is the reliable per-comment time; fall back to the
        # enclosing entry's timestamp only if the inner one is missing.
        ts = comment.get("timestamp")
        if ts is None:
            ts = entry.get("timestamp")
        out.append({
            "external_id": _fb_id("fbcomment:", ts, text),
            "title": _title(text),
            "url": None,
            "created_at": _iso_utc(ts),
            "updated_at": _iso_utc(ts),
            "meta": {
                "text": text,
                "social_source": SOURCE,
                "post_type": "comment",
                "reply_to_context": reply_to_context,
                "author": _demojibake(comment.get("author") or ""),
            },
        })
    return out


def parse_facebook_dyi(path: str | Path) -> FacebookDyiResult:
    """Parse a Facebook DYI export (ZIP path or extracted directory) into a
    ``FacebookDyiResult``. See module docstring for the record shapes."""
    counts = {"posts_seen": 0, "comments_seen": 0, "skipped_no_text": 0}
    posts: list[dict] = []
    comments: list[dict] = []
    authors: set[str] = set()

    with _archive(path) as (names, read):
        for name in names:
            if _POSTS_RE.search(name):
                data = _load_json(read(name))
                if isinstance(data, list):
                    for record in data:
                        rec = _post_record(record, counts)
                        if rec is not None:
                            posts.append(rec)
            elif _COMMENTS_RE.search(name):
                data = _load_json(read(name))
                if isinstance(data, dict):
                    for entry in (data.get("comments_v2") or []):
                        comments.extend(_comment_records(entry, counts, authors))

    # Count only — never the names (logging discipline). >1 flags a format
    # change that would break the self-authored-only assumption (see
    # _comment_records docstring).
    counts["comment_authors_seen"] = len(authors)
    return FacebookDyiResult(posts=posts, comments=comments, counts=counts)
