"""Parser for ChatGPT official export (conversations.json).

Format: a JSON array of conversations. Each conversation has a `mapping`
of node-id -> {message, parent, children}. Messages live in the mapping;
order is recovered by walking parent links from the leaves (or sorting by
create_time as a fallback). `content.parts` holds the text for normal
messages; other content_types (code, multimodal_text, ...) are handled
best-effort.

Attachments (P1-J follow-up): the export carries them in two channels.
- `content.parts[]` items with `asset_pointer = "file-service://file-XXX"`
  resolve to `file-XXX.dat` inside the ZIP — bytes are present.
- `metadata.attachments[]` carries UUID-keyed records whose bytes are NOT
  exported; we keep them as metadata-only attachments.
A sibling `conversation_asset_file_names.json` maps each `file-XXX.dat` to
its original filename ("スクリーンショット ….png"); we use it for source_ref.
"""
from __future__ import annotations

import hashlib
import os
from datetime import datetime, timezone

from .base import (
    ParseResult, ParsedAttachment, ParsedConversation, ParsedMessage,
    fallback_source_id, make_title,
)

SOURCE = "chatgpt"
# Per-parser version (backlog A3), recorded in import_runs as
# "<SOURCE>/<PARSER_VERSION>". Bump when this parser's output for the
# same input could change, then re-ingest this source only.
PARSER_VERSION = "1"

# Tells the dispatcher to pass us the ZIP's binary entries plus the asset-
# name lookup. Without these we'd still parse, just without bytes/filenames.
WANTS_ATTACHMENTS = True

# Asset pointers shaped "file-service://file-XXX" map 1:1 onto file-XXX.dat
# inside the ZIP. Other prefixes (sediment://, https://) we don't have
# bytes for and so record as metadata-only attachments.
_FILE_SERVICE = "file-service://"


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


_EXT_MIME = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".gif": "image/gif", ".webp": "image/webp", ".heic": "image/heic",
    ".pdf": "application/pdf",
    ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".wav": "audio/wav",
    ".mp4": "video/mp4", ".mov": "video/quicktime",
}


def _lookup_blob(attachments_map: dict[str, bytes] | None, dat_name: str) -> bytes | None:
    """Resolve `file-XXX.dat` against the zip member dict. Newer ChatGPT
    exports put .dat files at the archive root, so a direct hit is the
    common case; the slash suffix check is a courtesy for archives that
    nest them under a sub-prefix."""
    if not attachments_map:
        return None
    if dat_name in attachments_map:
        return attachments_map[dat_name]
    for k, v in attachments_map.items():
        if k.endswith("/" + dat_name):
            return v
    return None


def _attachments_from_message(
    msg: dict,
    attachments_map: dict[str, bytes] | None,
    asset_names: dict[str, str] | None,
) -> list[ParsedAttachment]:
    """Pull every attachment off one message.

    Two sources are folded together (order: asset_pointers then
    metadata.attachments[]) so a UI showing them in declaration order sees
    images first, then files. We dedupe nothing here — the export occasionally
    references the same asset_pointer twice in the mapping and we keep the
    duplicate so message_id linkage stays 1:1 with the source.
    """
    out: list[ParsedAttachment] = []
    content = msg.get("content") or {}
    for part in (content.get("parts") or []):
        if not isinstance(part, dict):
            continue
        ptr = part.get("asset_pointer")
        if not isinstance(ptr, str) or not ptr.startswith(_FILE_SERVICE):
            # audio (sediment://), real-time video container, etc. — we
            # don't have bytes for these so skip rather than fabricate.
            continue
        file_id = ptr[len(_FILE_SERVICE):]
        dat_name = f"{file_id}.dat"
        data = _lookup_blob(attachments_map, dat_name)
        friendly = (asset_names or {}).get(dat_name) or dat_name
        ext = os.path.splitext(friendly)[1].lower()
        mime = _EXT_MIME.get(ext)
        out.append(ParsedAttachment(
            source_ref=friendly,
            mime=mime,
            size=len(data) if data is not None else part.get("size_bytes"),
            hash=hashlib.sha256(data).hexdigest() if data is not None else None,
            data=data,
        ))
    for att in (msg.get("metadata") or {}).get("attachments") or []:
        if not isinstance(att, dict):
            continue
        out.append(ParsedAttachment(
            source_ref=att.get("name") or att.get("id"),
            mime=att.get("mime_type") or att.get("mimeType"),
            size=att.get("size"),
            # No bytes shipped for this channel (UUID-keyed uploads) → no
            # data/hash — the metadata row records "an upload happened" only.
        ))
    return out


def parse(
    data,
    *,
    attachments: dict[str, bytes] | None = None,
    asset_names: dict[str, str] | None = None,
) -> ParseResult:
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
                msg_attachments = _attachments_from_message(msg, attachments, asset_names)
                # A turn with no prose but a real attachment is still a turn
                # (the user dropped a screenshot in and said nothing); keep it.
                if not text and not msg_attachments:
                    continue
                messages.append(
                    ParsedMessage(
                        role=role,
                        text=text,
                        created_at=_ts(msg.get("create_time")),
                        source_message_id=msg.get("id"),
                        attachments=msg_attachments,
                    )
                )
            if not messages:
                continue
            source_id = conv.get("conversation_id") or conv.get("id") or fallback_source_id(
                messages, conv.get("title"), _ts(conv.get("create_time")),
            )
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
