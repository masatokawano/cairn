"""Cross-source recall: related() and weekly digest generation (M4, §5.3).

related() is the core of the weekly review's 「過去からの関連」 section (S2:
毎週1件以上の再発見): it takes this week's activity texts as queries, runs
the existing cross-source search (hybrid when embeddings are usable, keyword
otherwise), and returns only items OLDER than exclude_days — resurfacing is
about the past, so recent items are filtered out at the SQL level via the
search `before` parameter. Results carry a `reason` (which activity text
they reacted to, and how) so the review can say why each item came up.

weekly_activity() collects the last 7 days per source with the D6 caps; the
conversation noise rules are shared with deliver/auto_lists (旧
sync_cairn_recent.py の除外規則). weekly_digest() ties both together.

Nothing here writes to the DB — recall is a read-only consumer of the items
registry and the search indexes.
"""
from __future__ import annotations

import json
import math
import os
import re
from datetime import datetime, timedelta, timezone

from .. import db
from ..deliver.auto_lists import (
    EXCLUDED_EXACT_TITLES,
    EXCLUDED_TITLE_PREFIXES,
    _is_review_candidate,
    _parse_ts,
)

DEFAULT_K = 10                 # D6: 各セクション最大10件
DEFAULT_EXCLUDE_DAYS = 14      # §5.3: 「過去からの」なので直近2週間は除外
DEFAULT_ACTIVITY_DAYS = 7


def _cfg_int(name: str, default: int) -> int:
    return int(os.environ.get(name, str(default)))


def _utc(now: datetime | None) -> datetime:
    return now or datetime.now(timezone.utc)


def _is_noise_conversation(row: dict) -> bool:
    """Noise-title filter for search hits (related() must not resurface
    security-review boilerplate / untitled chats). Title rules only — the
    message-count half of _is_review_candidate needs a count that search
    rows don't carry."""
    if row.get("kind") != "conversation":
        return False
    title = (row.get("title") or "").strip()
    return (not title or title in EXCLUDED_EXACT_TITLES
            or title.startswith(EXCLUDED_TITLE_PREFIXES))


def related(
    query_texts: list[str],
    *,
    k: int | None = None,
    exclude_days: int | None = None,
    now: datetime | None = None,
    provider=None,
) -> list[dict]:
    """Items related to ``query_texts`` but older than ``exclude_days``.

    Per query: one db.search() call (hybrid if an embedding provider is
    resolvable, else keyword — the review must still work on an archive
    without embeddings, S4). Across queries: RRF fusion keyed by item, each
    item remembering the query it ranked best for (`reason`).

    Source diversity (§5.3 「同一ソース独占を避ける丸め」): no source may
    take more than ⌈k/2⌉ slots while other sources still have candidates;
    the cap is waived only to fill an otherwise short list.

    Returned rows are db.search() rows plus
    ``reason = {"query": <activity text>, "match_reason": keyword|semantic|both}``.
    """
    k = k if k is not None else _cfg_int("CAIRN_RELATED_K", DEFAULT_K)
    exclude_days = (
        exclude_days if exclude_days is not None
        else _cfg_int("CAIRN_RELATED_EXCLUDE_DAYS", DEFAULT_EXCLUDE_DAYS)
    )
    now = _utc(now)
    cutoff = (
        now.astimezone(timezone.utc) - timedelta(days=exclude_days)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")

    if provider is None:
        try:
            provider = db._active_embedding_provider()
        except Exception:
            provider = None  # no embeddings yet / no runtime → keyword only
    mode = "hybrid" if provider is not None else "keyword"

    fetch = max(k * 3, 20)  # room for the diversity rounding to choose from
    per_query: list[tuple[str, list[dict]]] = []
    for q in query_texts:
        q = " ".join((q or "").split())
        if not q:
            continue
        try:
            rows = db.search(q, mode=mode, provider=provider,
                             before=cutoff, limit=fetch)
        except Exception:
            if mode == "keyword":
                raise
            # embedding runtime broke mid-flight (model load, vec0, …):
            # degrade for the remaining queries instead of failing the review
            mode, provider = "keyword", None
            rows = db.search(q, mode="keyword", before=cutoff, limit=fetch)
        per_query.append((q, rows))

    # RRF across query lists; remember, per item, the query it ranked best
    # for — that pairing becomes the 「なぜ出したか」 line in the review.
    scores: dict = {}
    best: dict = {}
    rowmap: dict = {}
    for q, rows in per_query:
        for pos, row in enumerate(rows):
            if _is_noise_conversation(row):
                continue
            key = (
                row["item_id"] if row["item_id"] is not None
                else ("conv", row["conversation_id"])
            )
            scores[key] = scores.get(key, 0.0) + 1.0 / (db._RRF_K + pos + 1)
            if key not in best or pos < best[key][1]:
                best[key] = (q, pos, row["match_reason"])
                rowmap[key] = row  # keep the row from the winning query
    ranked = sorted(scores, key=lambda kk: scores[kk], reverse=True)

    cap = max(1, math.ceil(k / 2))
    picked: list = []
    overflow: list = []
    per_source: dict[str, int] = {}
    for key in ranked:
        if len(picked) >= k:
            break
        src = rowmap[key]["source"]
        if per_source.get(src, 0) < cap:
            per_source[src] = per_source.get(src, 0) + 1
            picked.append(key)
        else:
            overflow.append(key)
    for key in overflow:  # fill up only when other sources ran dry
        if len(picked) >= k:
            break
        picked.append(key)

    out: list[dict] = []
    for key in picked:
        row = dict(rowmap[key])
        q, _, match_reason = best[key]
        row["reason"] = {"query": q, "match_reason": match_reason}
        out.append(row)
    return out


def weekly_activity(
    *,
    now: datetime | None = None,
    days: int | None = None,
    max_items: int | None = None,
) -> dict:
    """This week's items per §5.4 section, capped at max_items each (D6).

    Sections: discoveries (Karakeep, to-review 優先, windowed on created_at
    = 保存日), thoughts (conversations, ≥4 messages + noise-title rules from
    auto_lists), references (Zotero, updated_at), notes (Obsidian, updated_at).
    The window is (now - days, now]; the upper bound matters when a --week
    reference point in the past is used.
    """
    days = days if days is not None else _cfg_int(
        "CAIRN_REVIEW_ACTIVITY_DAYS", DEFAULT_ACTIVITY_DAYS)
    max_items = max_items if max_items is not None else _cfg_int(
        "CAIRN_REVIEW_MAX_ITEMS", DEFAULT_K)
    now = _utc(now)
    cutoff = now - timedelta(days=days)
    conn = db.connect()

    def in_window(ts_str: str | None) -> bool:
        ts = _parse_ts(ts_str)
        return ts is not None and cutoff <= ts <= now

    def item_row(r, extra: dict | None = None) -> dict:
        out = {
            "item_id": r["id"] if "id" in r.keys() else r["item_id"],
            "kind": r["kind"],
            "source": r["source"],
            "title": r["title"],
            "url": r["url"] if "url" in r.keys() else None,
            "external_id": r["external_id"],
            "created_at": r["created_at"] if "created_at" in r.keys() else None,
            "updated_at": r["updated_at"] if "updated_at" in r.keys() else None,
            "meta": json.loads(r["meta"] or "{}"),
        }
        out.update(extra or {})
        return out

    # 発見: Karakeep bookmarks saved this week, to-review tagged ones first
    discoveries = []
    for r in conn.execute(
        "SELECT id, kind, source, title, url, external_id, created_at,"
        " updated_at, meta FROM items"
        " WHERE source='karakeep' AND kind='bookmark'"
        " ORDER BY created_at DESC"
    ).fetchall():
        if in_window(r["created_at"]):
            discoveries.append(item_row(r))
    discoveries.sort(
        key=lambda d: ("to-review" not in (d["meta"].get("tags") or []),),
    )  # stable sort: to-review first, created_at DESC preserved within groups
    discoveries = discoveries[:max_items]

    # 思考: conversations updated this week, 旧 sync_cairn_recent 除外規則
    thoughts = []
    for r in conn.execute(
        """SELECT c.id AS conversation_id, c.source, c.title, c.created_at,
                  c.updated_at, c.meta, c.source_id AS external_id,
                  i.id AS item_id, COUNT(m.id) AS message_count
           FROM conversations c
           LEFT JOIN messages m ON m.conversation_id = c.id
           LEFT JOIN items i ON i.source = c.source AND i.external_id = c.source_id
           GROUP BY c.id ORDER BY c.updated_at DESC"""
    ).fetchall():
        if not in_window(r["updated_at"]):
            continue
        if not _is_review_candidate(r["title"] or "", r["message_count"]):
            continue
        thoughts.append({
            "item_id": r["item_id"],
            "kind": "conversation",
            "source": r["source"],
            "title": r["title"],
            "url": None,
            "external_id": r["external_id"],
            "created_at": r["created_at"],
            "updated_at": r["updated_at"],
            "meta": json.loads(r["meta"] or "{}"),
            "conversation_id": r["conversation_id"],
            "message_count": r["message_count"],
        })
        if len(thoughts) >= max_items:
            break

    # 根拠 / 理解: plain updated_at windows
    def collect(source: str, kind: str) -> list[dict]:
        out = []
        for r in conn.execute(
            "SELECT id, kind, source, title, url, external_id, created_at,"
            " updated_at, meta FROM items WHERE source=? AND kind=?"
            " ORDER BY updated_at DESC", (source, kind),
        ).fetchall():
            if in_window(r["updated_at"]):
                out.append(item_row(r))
                if len(out) >= max_items:
                    break
        return out

    return {
        "since": cutoff.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "until": now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "days": days,
        "discoveries": discoveries,
        "thoughts": thoughts,
        "references": collect("zotero", "reference"),
        "notes": collect("obsidian", "note"),
    }


# 検索クエリ化 (§5.3 「activity を検索クエリ化し related() で取得」).
# Japanese titles carry no whitespace, so a whole title is one FTS phrase
# that only matches near-identical text. Alongside the full title (which the
# semantic arm uses well) we emit its content terms as separate queries; the
# cross-query RRF in related() then acts as an OR with rank fusion. The
# splitter is deliberately crude — separators, punctuation and the common
# particles below — because related() only needs recall, not linguistics.
_TERM_SPLIT = re.compile(
    r"[\s、。・．，:：;；,.()（）\[\]「」『』<>《》…!?！？/／｜|—\-‐–]+"
)
_PARTICLE_SPLIT = re.compile(
    r"の|を|に(?:ついて|おける|関する)?|は|が|と|で|へ|や|から|まで|より"
)
_MAX_TERMS_PER_TITLE = 3
_MAX_QUERIES = 60
# Pure-ASCII fragments this short ("on", "to", "T1") are prepositions and
# labels, not topics — they LIKE-match half the archive and produce reason
# lines like 今週の「on」に関連. CJK terms stay meaningful at 2 chars (設計).
_MIN_ASCII_TERM = 3
_ASCII_STOPWORDS = {
    "the", "and", "for", "with", "from", "this", "that", "not", "are", "was",
    "http", "https", "www", "com",
}


def _content_terms(title: str) -> list[str]:
    """Best-effort content words of a title (order preserved)."""
    out: list[str] = []
    for frag in _TERM_SPLIT.split(title):
        for term in _PARTICLE_SPLIT.split(frag):
            term = term.strip()
            if len(term) < 2 or term in out:
                continue
            if term.isascii() and (
                len(term) < _MIN_ASCII_TERM
                or term.lower() in _ASCII_STOPWORDS
            ):
                continue
            out.append(term)
    return out


def _query_texts(activity: dict, sections: tuple[str, ...]) -> list[str]:
    queries: list[str] = []
    seen: set[str] = set()

    def push(q: str) -> None:
        if q and q not in seen and len(queries) < _MAX_QUERIES:
            seen.add(q)
            queries.append(q)

    for section in sections:
        for row in activity[section]:
            title = " ".join((row.get("title") or "").split())
            if not title:
                continue
            push(title)
            for term in _content_terms(title)[:_MAX_TERMS_PER_TITLE]:
                push(term)
    return queries


def weekly_digest(
    *,
    now: datetime | None = None,
    k: int | None = None,
    exclude_days: int | None = None,
    provider=None,
) -> dict:
    """weekly_activity + the related-from-the-past list (§5.3 weekly digest).

    Queries are the activity titles (thoughts first — conversations carry
    the richest topical signal) plus their content terms, deduped. Items
    that ARE this week's activity are dropped from related() output as a
    belt-and-suspenders on top of the date exclusion.
    """
    activity = weekly_activity(now=now)
    sections = ("thoughts", "discoveries", "references", "notes")
    rel = related(_query_texts(activity, sections), k=k,
                  exclude_days=exclude_days, now=now, provider=provider)
    active_ids = {
        row["item_id"]
        for section in sections
        for row in activity[section]
        if row.get("item_id") is not None
    }
    rel = [r for r in rel if r.get("item_id") not in active_ids]
    return {"activity": activity, "related": rel}
