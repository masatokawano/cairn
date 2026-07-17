"""Common data structures shared by all parsers.

Parsers convert source-specific files into a list of ParsedConversation.
They must be tolerant: skip malformed entries instead of raising, and
collect warnings so the importer can surface them.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

# Suite-level parser version, recorded in import_runs so an import's output can
# be traced to the parser logic that produced it. Bump whenever parser output
# for the same input could change (per-parser versioning is a future refinement).
PARSER_VERSION = "1"


@dataclass
class ParsedAttachment:
    """File attached to a message. `hash` is sha256 of the raw decoded bytes
    (so the same file referenced twice dedups by hash, and an in-place edit
    is detected by the diff importer). `source_ref` is None for inline-
    embedded (base64) attachments since there is no path to point at;
    `extracted_text` is reserved for future OCR / PDF text extraction passes.

    `data` (P1-J): the raw bytes when the parser has them — gemini Takeout
    images, ChatGPT `file-*.dat` blobs. db.upsert_conversations stores
    these in the filesystem blob store keyed by hash; the field is dropped
    from memory before the row is written, so it adds no DB bloat. For
    metadata-only attachments (e.g. Claude's UUID-only references) `data`
    stays None and only the metadata persists.
    """
    source_ref: str | None = None
    mime: str | None = None
    size: int | None = None
    hash: str | None = None
    extracted_text: str | None = None
    data: bytes | None = None


@dataclass
class ParsedMessage:
    role: str  # "user" | "assistant" | "system" | "tool"
    text: str
    created_at: str | None = None  # ISO8601 string (UTC) or None
    source_message_id: str | None = None
    attachments: list[ParsedAttachment] = field(default_factory=list)


@dataclass
class ParsedConversation:
    source: str  # "chatgpt" | "claude" | "gemini" | "claude_cli" | "codex_cli"
    source_id: str  # stable id within the source
    title: str
    messages: list[ParsedMessage] = field(default_factory=list)
    created_at: str | None = None
    updated_at: str | None = None
    meta: dict = field(default_factory=dict)

    def content_hash(self) -> str:
        """Stable hash of the conversation content, used for diff imports.

        Attachments contribute to the hash ONLY when present, so messages
        without attachments produce the same hash as before P1-H — existing
        conversations don't all re-update on next sync.
        """
        items: list = []
        for m in self.messages:
            entry: list = [m.role, m.text, m.created_at]
            if m.attachments:
                entry.append([a.hash for a in m.attachments])
            items.append(entry)
        payload = json.dumps(items, ensure_ascii=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass
class ParseResult:
    conversations: list[ParsedConversation] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # パースできなかった入力単位の数 (backlog A2)。1 ファイル upload の全体
    # 失敗は呼び出し側が failed=1 を記録する; ここは複数シャード zip のように
    # 「入力の一部が丸ごと読めなかった」場合のみパーサ側で数える。パーサ内の
    # 寛容な per-entry skip (壊れた行など) は従来どおり warnings であって
    # failed ではない。
    failed: int = 0


def make_title(text: str, limit: int = 60) -> str:
    """Derive a title from the first line of a message."""
    line = text.strip().splitlines()[0] if text.strip() else "(untitled)"
    return line[:limit] + ("…" if len(line) > limit else "")


def fallback_source_id(messages: list[ParsedMessage], title: str | None = None,
                       created_at: str | None = None) -> str:
    """Stable synthetic source_id for export entries that lack a real uuid/id.

    Earlier code used `f"index-{i}"` which made the id depend on the file's
    listing order; a re-exported file with a different order would create
    duplicate conversation rows on the next import. Instead we hash the
    conversation's content (title + created_at + first message text), so the
    same conversation deterministically gets the same id across re-exports
    while two genuinely-different conversations almost never collide.

    Distinguished by the "fallback-" prefix from any real id format we've
    seen in exports — important for debugging "where did this id come from?".
    """
    first_text = messages[0].text if messages else ""
    payload = json.dumps([title or "", created_at or "", first_text], ensure_ascii=False)
    h = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"fallback-{h[:16]}"
