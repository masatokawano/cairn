"""Parser for Claude.ai export (conversations.json from the privacy export ZIP).

Format: a JSON array of conversations:
  {uuid, name, created_at, updated_at,
   chat_messages: [{uuid, sender: "human"|"assistant", text,
                    content: [{type: "text", text}], created_at}]}

`text` is usually the flat text; `content` blocks are the newer structured
form. Prefer `content` when present, fall back to `text`.
"""
from __future__ import annotations

from .base import ParseResult, ParsedConversation, ParsedMessage, make_title

SOURCE = "claude"

_ROLE = {"human": "user", "assistant": "assistant"}


def looks_like(data) -> bool:
    return (
        isinstance(data, list)
        and len(data) > 0
        and isinstance(data[0], dict)
        and "chat_messages" in data[0]
    )


def _message_text(msg: dict) -> str:
    blocks = msg.get("content")
    if isinstance(blocks, list) and blocks:
        chunks = []
        for b in blocks:
            if isinstance(b, dict) and isinstance(b.get("text"), str):
                chunks.append(b["text"])
        text = "\n".join(c for c in chunks if c.strip()).strip()
        if text:
            return text
    text = msg.get("text")
    return text.strip() if isinstance(text, str) else ""


def parse(data) -> ParseResult:
    result = ParseResult()
    if not isinstance(data, list):
        result.warnings.append("claude: top-level JSON is not a list")
        return result

    for i, conv in enumerate(data):
        if not isinstance(conv, dict) or "chat_messages" not in conv:
            result.warnings.append(f"claude: entry {i} has no chat_messages, skipped")
            continue
        try:
            messages = []
            for msg in conv["chat_messages"] or []:
                role = _ROLE.get(msg.get("sender", ""))
                if not role:
                    continue
                text = _message_text(msg)
                if not text:
                    continue
                messages.append(
                    ParsedMessage(
                        role=role,
                        text=text,
                        created_at=msg.get("created_at"),
                        source_message_id=msg.get("uuid"),
                    )
                )
            if not messages:
                continue
            source_id = conv.get("uuid") or f"index-{i}"
            result.conversations.append(
                ParsedConversation(
                    source=SOURCE,
                    source_id=str(source_id),
                    title=conv.get("name") or make_title(messages[0].text),
                    messages=messages,
                    created_at=conv.get("created_at"),
                    updated_at=conv.get("updated_at"),
                )
            )
        except Exception as e:  # noqa: BLE001
            result.warnings.append(f"claude: entry {i} failed: {e}")
    return result
