"""Parser for codex CLI session logs (~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl).

Each file is one session. Line types:
- session_meta: payload has id, timestamp, cwd
- response_item: payload.type=="message" with role user/assistant and
  content blocks of input_text / output_text
- event_msg, turn_context, ...: ignored

The first "user" messages are often injected boilerplate (permissions,
AGENTS.md instructions, environment_context) — filtered by prefix.
"""
from __future__ import annotations

import json

from .base import ParseResult, ParsedConversation, ParsedMessage, make_title

SOURCE = "codex_cli"

_NOISE_PREFIXES = (
    "<permissions instructions>",
    "<environment_context>",
    "<user_instructions>",
    "<turn_context>",
    "# AGENTS.md instructions",
    "<ENVIRONMENT_CONTEXT>",
)


_IDE_WRAPPER = "# Context from my IDE setup"
_IDE_REQUEST_MARKER = "## My request for Codex:"


def _clean_text(t: str) -> str:
    """Strip injected boilerplate; unwrap the IDE-context wrapper."""
    t = t.strip()
    if t.startswith(_NOISE_PREFIXES):
        return ""
    if t.startswith(_IDE_WRAPPER):
        # Real request is embedded after the marker; the rest is IDE noise.
        _, sep, request = t.partition(_IDE_REQUEST_MARKER)
        return request.strip() if sep else ""
    return t


def _content_text(content) -> str:
    if isinstance(content, str):
        return _clean_text(content)
    if not isinstance(content, list):
        return ""
    chunks = []
    for b in content:
        if isinstance(b, dict) and b.get("type") in ("input_text", "output_text", "text"):
            t = b.get("text", "")
            if isinstance(t, str):
                t = _clean_text(t)
                if t:
                    chunks.append(t)
    return "\n".join(chunks)


def parse_file(path: str, content: str) -> ParseResult:
    result = ParseResult()
    messages: list[ParsedMessage] = []
    session_id = None
    cwd = None
    created_at = None
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
        payload = rec.get("payload") or {}
        rtype = rec.get("type")
        if rtype == "session_meta":
            session_id = payload.get("id") or session_id
            cwd = payload.get("cwd") or cwd
            created_at = payload.get("timestamp") or rec.get("timestamp") or created_at
            continue
        if rtype != "response_item" or payload.get("type") != "message":
            continue
        role = payload.get("role")
        if role not in ("user", "assistant"):
            continue
        text = _content_text(payload.get("content"))
        if not text:
            continue
        ts = rec.get("timestamp")
        last_ts = ts or last_ts
        messages.append(
            ParsedMessage(role=role, text=text, created_at=ts)
        )

    if not messages:
        return result

    first_user = next((m for m in messages if m.role == "user"), messages[0])
    result.conversations.append(
        ParsedConversation(
            source=SOURCE,
            source_id=session_id or path,
            title=make_title(first_user.text),
            messages=messages,
            created_at=created_at or messages[0].created_at,
            updated_at=last_ts,
            meta={"cwd": cwd, "path": path},
        )
    )
    return result
