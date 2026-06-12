"""Parser for Gemini history via Google Takeout "My Activity" (MyActivity.json).

Format: a JSON array of activity records:
  {header: "Gemini Apps", title: "Prompted <text>", time: ISO8601,
   subtitles: [...], ...}

Caveats (see README/NOTES):
- My Activity has NO thread structure — each record is a single prompt,
  so each becomes its own one-or-two-message conversation here.
- The JSON variant historically contains only the prompt; responses may
  appear in some export versions under varying keys. We look in a few
  known places and otherwise import the prompt alone.
- Titles are locale-dependent ("Prompted X" / 「X と入力しました」など),
  so prefix stripping is best-effort.
"""
from __future__ import annotations

import hashlib

from .base import ParseResult, ParsedConversation, ParsedMessage, make_title

SOURCE = "gemini"

_PROMPT_PREFIXES = ("Prompted ", "Asked ")
_PROMPT_SUFFIXES = (" と入力しました", "と入力しました")


def looks_like(data) -> bool:
    return (
        isinstance(data, list)
        and len(data) > 0
        and isinstance(data[0], dict)
        and "header" in data[0]
        and "time" in data[0]
    )


def _prompt_from_title(title: str) -> str:
    for p in _PROMPT_PREFIXES:
        if title.startswith(p):
            return title[len(p):].strip()
    for s in _PROMPT_SUFFIXES:
        if title.endswith(s):
            return title[: -len(s)].strip()
    return title.strip()


def _find_response(item: dict) -> str:
    """Best-effort: some export versions embed the model response."""
    for key in ("attachedResponse", "response", "details"):
        v = item.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    subs = item.get("subtitles")
    if isinstance(subs, list):
        texts = [
            s["name"].strip()
            for s in subs
            if isinstance(s, dict) and isinstance(s.get("name"), str) and s["name"].strip()
        ]
        if texts:
            return "\n".join(texts)
    return ""


def parse(data) -> ParseResult:
    result = ParseResult()
    if not isinstance(data, list):
        result.warnings.append("gemini: top-level JSON is not a list")
        return result

    skipped_headers: set[str] = set()
    for i, item in enumerate(data):
        if not isinstance(item, dict):
            continue
        header = item.get("header", "")
        if "Gemini" not in header and "Bard" not in header:
            skipped_headers.add(header)
            continue
        try:
            title_raw = item.get("title") or ""
            prompt = _prompt_from_title(title_raw)
            if not prompt:
                continue
            time = item.get("time")
            messages = [
                ParsedMessage(role="user", text=prompt, created_at=time)
            ]
            response = _find_response(item)
            if response:
                messages.append(
                    ParsedMessage(role="assistant", text=response, created_at=time)
                )
            digest = hashlib.sha256(
                f"{time}|{title_raw}".encode("utf-8")
            ).hexdigest()[:16]
            result.conversations.append(
                ParsedConversation(
                    source=SOURCE,
                    source_id=f"{time}-{digest}",
                    title=make_title(prompt),
                    messages=messages,
                    created_at=time,
                    updated_at=time,
                )
            )
        except Exception as e:  # noqa: BLE001
            result.warnings.append(f"gemini: entry {i} failed: {e}")
    if skipped_headers:
        result.warnings.append(
            "gemini: skipped non-Gemini headers: " + ", ".join(sorted(skipped_headers))
        )
    return result
