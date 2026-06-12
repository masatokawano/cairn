"""Common data structures shared by all parsers.

Parsers convert source-specific files into a list of ParsedConversation.
They must be tolerant: skip malformed entries instead of raising, and
collect warnings so the importer can surface them.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field


@dataclass
class ParsedMessage:
    role: str  # "user" | "assistant" | "system" | "tool"
    text: str
    created_at: str | None = None  # ISO8601 string (UTC) or None
    source_message_id: str | None = None


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
        """Stable hash of the conversation content, used for diff imports."""
        payload = json.dumps(
            [(m.role, m.text, m.created_at) for m in self.messages],
            ensure_ascii=False,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass
class ParseResult:
    conversations: list[ParsedConversation] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def make_title(text: str, limit: int = 60) -> str:
    """Derive a title from the first line of a message."""
    line = text.strip().splitlines()[0] if text.strip() else "(untitled)"
    return line[:limit] + ("…" if len(line) > limit else "")
