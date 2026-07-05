"""90 Auto list generation (M3, DESIGN.md §5.5) — content only, no I/O.

Produces the four indexes the legacy brain-sync scripts wrote hourly:
karakeep-to-review.md / cairn-recent.md / zotero-recent.md /
obsidian-context.md. Formats follow the legacy output (§5.5: フォーマットは
旧仕様を踏襲してよい); data now comes straight from cairn.db instead of
shelling out to APIs.

This module returns {filename: markdown}. Writing to the vault is the
exclusive job of deliver/obsidian_writer.py (invariant 2) — keeping
generation pure makes the formats unit-testable without touching a vault.

External-origin text (titles, tags, authors) is untrusted (§6.1): it is
collapsed to a single line before being placed into markdown so it cannot
break out of its list entry, forge frontmatter, or inject headings.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

from .. import db

CAIRN_LOOKBACK_DAYS = 7
ZOTERO_LOOKBACK_DAYS = 7
OBSIDIAN_LOOKBACK_DAYS = 30

# 旧 sync_cairn_recent.py の除外規則（§5.3 は M4 の weekly_activity でも再利用）
EXCLUDED_TITLE_PREFIXES = (
    "Review this change for security vulnerabilities.",
    "You are a security expert reviewing",
)
EXCLUDED_EXACT_TITLES = {"New chat", "User Request: Help Needed", "Untitled"}
MIN_MESSAGES = 4


# Inline-markdown metacharacters neutralised in untrusted text (§6.1 /
# Codex M3 review should #2). Newline collapse alone stops frontmatter and
# heading forgery but not same-line constructs: [click](url), ![img](url),
# `code`, emphasis. '#' is only structural at line start, which the collapse
# already prevents.
_MD_META = re.compile(r"([\\`*_\[\]()<>!|])")


def _esc(value: str | None) -> str:
    """Untrusted text → one line, inline markdown metacharacters escaped.
    For prose positions (headings, list values)."""
    collapsed = " ".join((value or "").split())
    return _MD_META.sub(r"\\\1", collapsed)


def _esc_code(value: str | None) -> str:
    """Untrusted text → one line, safe inside a `code span` (backslash
    escapes don't work there, so backticks are replaced instead)."""
    return " ".join((value or "").split()).replace("`", "'")


def _url(value: str | None) -> str | None:
    """Untrusted URL → autolink-safe form, or None to omit the line.
    <...> wrapping stops inline-markdown parsing; '<'/'>' are invalid in
    URLs and dropped so the wrapper can't be closed early. Non-http(s)
    schemes are omitted entirely (same policy as db._safe_external_url)."""
    collapsed = "".join((value or "").split())
    collapsed = collapsed.replace("<", "").replace(">", "")
    if not re.match(r"^https?://", collapsed, re.IGNORECASE):
        return None
    return f"<{collapsed}>"


def _wikilink(rel: str) -> str:
    """Vault-relative path → [[wikilink]] text. Obsidian link syntax cannot
    escape ']]' / '|' / '#', so paths containing them fall back to escaped
    plain text (the link would be broken anyway)."""
    if any(ch in rel for ch in "[]|#"):
        return _esc(rel)
    return f"[[{rel}]]"


def _now_local(now: datetime | None) -> datetime:
    return (now or datetime.now(timezone.utc)).astimezone()


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def karakeep_to_review(now: datetime | None = None) -> str:
    conn = db.connect()
    rows = conn.execute(
        "SELECT external_id, title, url, created_at, meta FROM items"
        " WHERE source='karakeep' AND kind='bookmark'"
        " ORDER BY created_at DESC"
    ).fetchall()
    picked = []
    for r in rows:
        meta = json.loads(r["meta"] or "{}")
        if "to-review" in (meta.get("tags") or []):
            picked.append((r, meta))
    lines = [
        "---",
        "source: karakeep",
        "type: review-index",
        f"generated: {_now_local(now):%Y-%m-%d %H:%M:%S}",
        f"item_count: {len(picked)}",
        "---",
        "",
        "# Karakeep — 要レビュー",
        "",
        "Karakeepで `to-review` タグを付けた項目の自動一覧です。",
        "",
    ]
    for r, meta in picked:
        lines.extend([
            f"## {_esc(r['title']) or '無題'}",
            "",
        ])
        url = _url(r["url"])
        if url:
            lines.append(f"- URL: {url}")
        lines.extend([
            f"- Karakeep ID: `{_esc_code(r['external_id'])}`",
            f"- 保存日時: {_esc(r['created_at'])}",
            f"- タグ: {_esc(', '.join(meta.get('tags') or []))}",
            "",
            "- [ ] 内容を確認",
            "- [ ] Zoteroへ昇格",
            "- [ ] Obsidianのテーマへ反映",
            "",
            "---",
        ])
    return "\n".join(lines) + "\n"


def _is_review_candidate(title: str, message_count: int) -> bool:
    title = title.strip()
    if not title or title in EXCLUDED_EXACT_TITLES:
        return False
    if title.startswith(EXCLUDED_TITLE_PREFIXES):
        return False
    return message_count >= MIN_MESSAGES


def cairn_recent(now: datetime | None = None) -> str:
    conn = db.connect()
    cutoff = (
        (now or datetime.now(timezone.utc)) - timedelta(days=CAIRN_LOOKBACK_DAYS)
    )
    rows = conn.execute(
        """SELECT c.id, c.source, c.title, c.updated_at, c.meta,
                  COUNT(m.id) AS message_count
           FROM conversations c LEFT JOIN messages m ON m.conversation_id = c.id
           GROUP BY c.id ORDER BY c.updated_at DESC"""
    ).fetchall()
    recent = []
    for r in rows:
        ts = _parse_ts(r["updated_at"])
        if ts is None or ts < cutoff:
            continue
        if _is_review_candidate(r["title"] or "", r["message_count"]):
            recent.append(r)
    lines = [
        "---",
        "source: cairn",
        "type: recent-conversations-index",
        f"generated: {_now_local(now):%Y-%m-%d %H:%M:%S}",
        f"lookback_days: {CAIRN_LOOKBACK_DAYS}",
        f"item_count: {len(recent)}",
        "---",
        "",
        "# Cairn — 直近7日間の会話",
        "",
        "Cairnに保存された最近の生成AI対話の自動一覧です。",
        "",
    ]
    for r in recent:
        cwd = (json.loads(r["meta"] or "{}") or {}).get("cwd")
        lines.extend([
            f"## {_esc(r['title']) or '無題'}",
            "",
            f"- Source: `{_esc_code(r['source'])}`",
            f"- Cairn ID: `{r['id']}`",
            f"- 更新日時: {_esc(r['updated_at'])}",
            f"- メッセージ数: {r['message_count']}",
        ])
        if cwd:
            lines.append(f"- Project: `{_esc_code(cwd)}`")
        lines.extend([
            "",
            "- [ ] 内容を確認",
            "- [ ] Obsidianへ反映",
            "- [ ] 未解決課題を抽出",
            "",
            "---",
            "",
        ])
    return "\n".join(lines) + "\n"


def zotero_recent(now: datetime | None = None) -> str:
    conn = db.connect()
    cutoff = (
        (now or datetime.now(timezone.utc)) - timedelta(days=ZOTERO_LOOKBACK_DAYS)
    )
    rows = conn.execute(
        "SELECT external_id, title, url, doi, updated_at, meta FROM items"
        " WHERE source='zotero' AND kind='reference'"
        " ORDER BY updated_at DESC"
    ).fetchall()
    recent = []
    for r in rows:
        ts = _parse_ts(r["updated_at"])
        if ts is not None and ts >= cutoff:
            recent.append(r)
    lines = [
        "---",
        "source: zotero",
        "type: recent-items-index",
        f"generated: {_now_local(now):%Y-%m-%d %H:%M:%S}",
        f"lookback_days: {ZOTERO_LOOKBACK_DAYS}",
        f"item_count: {len(recent)}",
        "---",
        "",
        "# Zotero — 直近7日間の資料",
        "",
        "Zoteroで最近追加または更新された資料の自動一覧です。",
        "",
    ]
    for r in recent:
        meta = json.loads(r["meta"] or "{}")
        creators = ", ".join(meta.get("creators", [])[:5])
        tags = ", ".join(meta.get("tags", []))
        lines.extend([
            f"## {_esc(r['title']) or '無題'}",
            "",
            f"- 種別: `{_esc_code(meta.get('itemType')) or 'unknown'}`",
            f"- Zotero Key: `{_esc_code(r['external_id'])}`",
            f"- 更新日時: {_esc(r['updated_at'])}",
        ])
        if creators:
            lines.append(f"- 著者: {_esc(creators)}")
        if r["doi"]:
            lines.append(f"- DOI: `{_esc_code(r['doi'])}`")
        url = _url(r["url"])
        if url:
            lines.append(f"- URL: {url}")
        if tags:
            lines.append(f"- タグ: {_esc(tags)}")
        lines.extend([
            "",
            "- [ ] 内容を確認",
            "- [ ] Obsidianのテーマへ反映",
            "- [ ] Cairnの関連対話を探す",
            "",
            "---",
            "",
        ])
    return "\n".join(lines) + "\n"


def obsidian_context(now: datetime | None = None) -> str:
    conn = db.connect()
    cutoff = (
        (now or datetime.now(timezone.utc)) - timedelta(days=OBSIDIAN_LOOKBACK_DAYS)
    )

    def collect(folder_prefix: str) -> list:
        rows = conn.execute(
            "SELECT external_id, title, updated_at, meta FROM items"
            " WHERE source='obsidian' AND kind='note'"
            " ORDER BY updated_at DESC"
        ).fetchall()
        out = []
        for r in rows:
            meta = json.loads(r["meta"] or "{}")
            if not (meta.get("folder") or "").startswith(folder_prefix):
                continue
            ts = _parse_ts(r["updated_at"])
            if ts is not None and ts >= cutoff:
                out.append((r, ts))
        return out

    themes = collect("10 Themes")
    projects = collect("20 Projects")
    lines = [
        "---",
        "source: obsidian",
        "type: current-context-index",
        f"generated: {_now_local(now):%Y-%m-%d %H:%M:%S}",
        f"lookback_days: {OBSIDIAN_LOOKBACK_DAYS}",
        f"theme_count: {len(themes)}",
        f"project_count: {len(projects)}",
        "---",
        "",
        "# Obsidian — 現在の理解",
        "",
        "最近更新されたテーマノートとプロジェクトノートの自動一覧です。",
        "",
        "## Themes",
        "",
    ]

    def link_line(row, ts) -> str:
        # external_id is the vault-relative path; strip .md for the wikilink
        rel = row["external_id"]
        rel = rel[:-3] if rel.endswith(".md") else rel
        rel = " ".join(rel.split())  # collapse first; _wikilink handles the rest
        return f"- {_wikilink(rel)} — 更新 {ts.astimezone():%Y-%m-%d %H:%M}"

    if themes:
        lines.extend(link_line(r, ts) for r, ts in themes)
    else:
        lines.append("_最近更新されたテーマノートはありません。_")
    lines.extend(["", "## Projects", ""])
    if projects:
        lines.extend(link_line(r, ts) for r, ts in projects)
    else:
        lines.append("_最近更新されたプロジェクトノートはありません。_")
    lines.extend([
        "",
        "## Review",
        "",
        "- [ ] 現在のテーマを確認",
        "- [ ] 進行中プロジェクトを確認",
        "- [ ] 今週の資料・対話との関連を確認",
        "",
    ])
    return "\n".join(lines)


def generate_all(now: datetime | None = None) -> dict[str, str]:
    """All four 90 Auto lists as {filename: markdown}."""
    return {
        "karakeep-to-review.md": karakeep_to_review(now),
        "cairn-recent.md": cairn_recent(now),
        "zotero-recent.md": zotero_recent(now),
        "obsidian-context.md": obsidian_context(now),
    }
