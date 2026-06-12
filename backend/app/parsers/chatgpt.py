"""Parser for ChatGPT official export (conversations.json).

Format: a JSON array of conversations. Each conversation has a `mapping`
of node-id -> {message, parent, children}. Messages live in the mapping;
order is recovered by walking parent links from the leaves (or sorting by
create_time as a fallback). `content.parts` holds the text for normal
messages; other content_types (code, multimodal_text, ...) are handled
best-effort.
"""
from __future__ import annotations

from datetime import datetime, timezone

from .base import ParseResult, ParsedConversation, ParsedMessage, make_title

SOURCE = "chatgpt"


def looks_like(data) -> bool:
    return (
        isinstance(data, list)
        and len(data) > 0
        and isinstance(data[0], dict)
        and "mapping" in data[0]
    )


def _ts(value) -> str | None:
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()
    except (ValueError, TypeError, OSError):
        return None


def _extract_text(content: dict) -> str:
    """Pull readable text out of a message content object, best-effort."""
    if not isinstance(content, dict):
        return ""
    ctype = content.get("content_type", "text")
    if ctype in ("text", "multimodal_text"):
        parts = content.get("parts") or []
        chunks = []
        for p in parts:
            if isinstance(p, str):
                chunks.append(p)
            elif isinstance(p, dict):
                # image/audio pointers etc. — keep a marker so context survives
                inner = p.get("text") or p.get("content")
                chunks.append(inner if isinstance(inner, str) else "")
        return "\n".join(c for c in chunks if c).strip()
    if ctype == "code":
        return (content.get("text") or "").strip()
    if ctype == "user_editable_context":
        return ""  # custom instructions boilerplate, not conversation content
    # thoughts / reasoning recaps / unknown types: try common fields
    text = content.get("text")
    return text.strip() if isinstance(text, str) else ""


def _ordered_nodes(mapping: dict, current_node: str | None) -> list[dict]:
    """Return message nodes in conversation order.

    Prefer walking parent links from current_node (the active branch).
    Fall back to create_time sort over all nodes if the chain is broken.
    """
    if current_node and current_node in mapping:
        chain, node_id, seen = [], current_node, set()
        while node_id and node_id in mapping and node_id not in seen:
            seen.add(node_id)
            chain.append(mapping[node_id])
            node_id = mapping[node_id].get("parent")
        return list(reversed(chain))
    nodes = [n for n in mapping.values() if isinstance(n, dict)]
    nodes.sort(
        key=lambda n: ((n.get("message") or {}).get("create_time") or 0)
    )
    return nodes


def parse(data) -> ParseResult:
    result = ParseResult()
    if not isinstance(data, list):
        result.warnings.append("chatgpt: top-level JSON is not a list")
        return result

    for i, conv in enumerate(data):
        if not isinstance(conv, dict) or "mapping" not in conv:
            result.warnings.append(f"chatgpt: entry {i} has no mapping, skipped")
            continue
        try:
            messages = []
            for node in _ordered_nodes(conv["mapping"], conv.get("current_node")):
                msg = node.get("message")
                if not msg:
                    continue
                role = (msg.get("author") or {}).get("role", "")
                if role not in ("user", "assistant"):
                    continue
                if (msg.get("metadata") or {}).get("is_visually_hidden_from_conversation"):
                    continue
                text = _extract_text(msg.get("content") or {})
                if not text:
                    continue
                messages.append(
                    ParsedMessage(
                        role=role,
                        text=text,
                        created_at=_ts(msg.get("create_time")),
                        source_message_id=msg.get("id"),
                    )
                )
            if not messages:
                continue
            source_id = conv.get("conversation_id") or conv.get("id") or f"index-{i}"
            result.conversations.append(
                ParsedConversation(
                    source=SOURCE,
                    source_id=str(source_id),
                    title=conv.get("title") or make_title(messages[0].text),
                    messages=messages,
                    created_at=_ts(conv.get("create_time")),
                    updated_at=_ts(conv.get("update_time")),
                )
            )
        except Exception as e:  # noqa: BLE001 — one bad conversation must not kill the import
            result.warnings.append(f"chatgpt: entry {i} failed: {e}")
    return result
