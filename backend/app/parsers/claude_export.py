"""Parser for Claude.ai export (conversations.json from the privacy export ZIP).

Format: a JSON array of conversations:
  {uuid, name, created_at, updated_at,
   chat_messages: [{uuid, sender: "human"|"assistant", text,
                    content: [{type: "text", text}], created_at,
                    attachments: [{file_name, file_size, file_type,
                                   extracted_content}],
                    files: [{file_uuid, file_name}]}]}

`text` is usually the flat text; `content` blocks are the newer structured
form. Prefer `content` when present, fall back to `text`.

Attachments live at the **message** level, not inside content blocks:
- `attachments[]` carries text-like uploads with the text already extracted
  by Claude (file_type=txt/md/csv/...). We keep `extracted_content` as
  `extracted_text` so it is searchable, and hash it for diff detection.
- `files[]` carries UUID references whose bytes are not in the export. We
  record `source_ref` only (no hash, no extracted text).
"""
from __future__ import annotations

import hashlib

from .base import (
    ParseResult, ParsedAttachment, ParsedConversation, ParsedMessage,
    fallback_source_id, make_title,
)

SOURCE = "claude"

_ROLE = {"human": "user", "assistant": "assistant"}

# Best-effort MIME for the file_type strings Claude.ai uses. Unknown → None.
_MIME_BY_EXT = {
    "txt": "text/plain",
    "md": "text/markdown",
    "csv": "text/csv",
    "tsv": "text/tab-separated-values",
    "json": "application/json",
    "yaml": "application/yaml",
    "yml": "application/yaml",
    "xml": "application/xml",
    "html": "text/html",
    "pdf": "application/pdf",
    "py": "text/x-python",
    "js": "application/javascript",
    "ts": "application/typescript",
}


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


def _message_attachments(msg: dict) -> list[ParsedAttachment]:
    """Translate message.attachments[] + message.files[] into ParsedAttachment.

    `attachments[]` items embed `extracted_content` (text already pulled out
    of the file by Claude). We hash that text for diff detection — the raw
    bytes are not in the export, so the hash is over the extracted text and
    only signals "the extracted text changed", which is what diff import
    needs. `files[]` items carry only a UUID reference."""
    out: list[ParsedAttachment] = []
    for a in msg.get("attachments") or []:
        if not isinstance(a, dict):
            continue
        ec = a.get("extracted_content")
        if isinstance(ec, list):
            ec = "\n".join(x for x in ec if isinstance(x, str))
        if not isinstance(ec, str):
            ec = None
        ftype = a.get("file_type")
        mime = _MIME_BY_EXT.get(ftype.lower()) if isinstance(ftype, str) else None
        size = a.get("file_size") if isinstance(a.get("file_size"), int) else None
        h = hashlib.sha256(ec.encode("utf-8")).hexdigest() if ec else None
        out.append(ParsedAttachment(
            source_ref=a.get("file_name") if isinstance(a.get("file_name"), str) else None,
            mime=mime,
            size=size,
            hash=h,
            extracted_text=ec,
        ))
    for f in msg.get("files") or []:
        if not isinstance(f, dict):
            continue
        uuid = f.get("file_uuid") if isinstance(f.get("file_uuid"), str) else None
        name = f.get("file_name") if isinstance(f.get("file_name"), str) else None
        ref = uuid or name
        if not ref:
            continue
        out.append(ParsedAttachment(source_ref=ref))
    return out


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
                attachments = _message_attachments(msg)
                # Keep messages that have text OR attachments — a turn with
                # just an upload and no prose is still a real turn.
                if not text and not attachments:
                    continue
                messages.append(
                    ParsedMessage(
                        role=role,
                        text=text,
                        created_at=msg.get("created_at"),
                        source_message_id=msg.get("uuid"),
                        attachments=attachments,
                    )
                )
            if not messages:
                continue
            source_id = conv.get("uuid") or fallback_source_id(
                messages, conv.get("name"), conv.get("created_at"),
            )
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
