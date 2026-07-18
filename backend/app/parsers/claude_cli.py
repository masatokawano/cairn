"""Parser for claude CLI session logs (~/.claude/projects/**/*.jsonl).

Each file is one session. Relevant lines have type "user" or "assistant"
with a `message` object; everything else (mode, file-history-snapshot,
attachment, ...) is noise. Notes:
- isSidechain=true lines are subagent traffic — skipped.
- message.content is either a string or a list of blocks; "text" blocks are
  imported as message text, "document" blocks become attachments (mime/
  size/hash from the decoded bytes; bytes themselves NOT stored).
  thinking / tool_use / tool_result are skipped.
- A "summary" line, when present, provides a human-readable title.
- cwd identifies the project; stored in meta.

Note: the `type:"attachment"` ROW kind in these logs is NOT a file
attachment — it carries tool/hook metadata (allowedTools, MCP servers,
skill counts, ...). Real file attachments live inside message.content.
"""
from __future__ import annotations

import base64
import hashlib
import json

from .base import ParseResult, ParsedAttachment, ParsedConversation, ParsedMessage, make_title

SOURCE = "claude_cli"
# Per-parser version (backlog A3), recorded in import_runs as
# "<SOURCE>/<PARSER_VERSION>". Bump when this parser's output for the
# same input could change, then re-ingest this source only.
PARSER_VERSION = "1"


def _block_text(content) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        chunks = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                t = b.get("text", "")
                if isinstance(t, str) and t.strip():
                    chunks.append(t.strip())
        return "\n".join(chunks)
    return ""


def _block_attachments(content) -> list[ParsedAttachment]:
    """Extract document/image blocks as ParsedAttachment (metadata only —
    the decoded bytes are hashed and counted, then discarded)."""
    if not isinstance(content, list):
        return []
    out: list[ParsedAttachment] = []
    for b in content:
        if not isinstance(b, dict):
            continue
        if b.get("type") not in ("document", "image"):
            continue
        src = b.get("source") or {}
        if src.get("type") == "base64" and isinstance(src.get("data"), str):
            try:
                raw = base64.b64decode(src["data"], validate=False)
            except (ValueError, TypeError):
                continue  # malformed → skip without raising
            out.append(ParsedAttachment(
                source_ref=None,  # inline-embedded; no path
                mime=src.get("media_type"),
                size=len(raw),
                hash=hashlib.sha256(raw).hexdigest(),
            ))
        elif isinstance(src.get("url"), str):
            # url-referenced attachments: record the ref, no hash without the bytes
            out.append(ParsedAttachment(
                source_ref=src["url"], mime=src.get("media_type"),
            ))
    return out


def _is_noise_user_text(text: str) -> bool:
    """Command wrappers and injected reminders, not real user input."""
    t = text.lstrip()
    return t.startswith((
        "<command-",            # <command-name>, <command-message>, ...
        "<local-command-",
        "<system-reminder>",
        "Caveat: The messages below",
    ))


def parse_file(path: str, content: str) -> ParseResult:
    """Parse one session JSONL file (content passed in, already read)."""
    result = ParseResult()
    messages: list[ParsedMessage] = []
    session_id = None
    cwd = None
    summary_title = None
    first_ts = None
    last_ts = None

    for ln, line in enumerate(content.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            result.warnings.append(f"{path}:{ln}: bad JSON line, skipped")
            continue
        if not isinstance(rec, dict):
            continue
        rtype = rec.get("type")
        if rtype == "summary" and rec.get("summary"):
            summary_title = rec["summary"]
            continue
        if rtype not in ("user", "assistant"):
            continue
        if rec.get("isSidechain"):
            continue
        if rec.get("isMeta"):
            continue
        msg = rec.get("message")
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        text = _block_text(content)
        attachments = _block_attachments(content)
        # Keep messages that have either text OR attachments — an
        # attachment-only message (e.g. a user pasting just a PDF) is a real
        # turn in the conversation.
        if not text and not attachments:
            continue
        if rtype == "user" and text and _is_noise_user_text(text):
            continue
        session_id = session_id or rec.get("sessionId")
        cwd = cwd or rec.get("cwd")
        ts = rec.get("timestamp")
        first_ts = first_ts or ts
        last_ts = ts or last_ts
        messages.append(
            ParsedMessage(
                role=rtype,
                text=text,
                created_at=ts,
                source_message_id=rec.get("uuid"),
                attachments=attachments,
            )
        )

    if not messages:
        return result

    first_user = next((m for m in messages if m.role == "user"), messages[0])
    result.conversations.append(
        ParsedConversation(
            source=SOURCE,
            source_id=session_id or path,
            title=summary_title or make_title(first_user.text),
            messages=messages,
            created_at=first_ts,
            updated_at=last_ts,
            meta={"cwd": cwd, "path": path},
        )
    )
    return result
