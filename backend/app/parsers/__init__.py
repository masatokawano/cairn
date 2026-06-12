"""Parser registry and auto-detection for uploaded export files."""
from __future__ import annotations

import io
import json
import zipfile

from . import chatgpt, claude_export, gemini
from .base import ParseResult

# Detection order matters: chatgpt's `mapping` and claude's `chat_messages`
# are unambiguous; gemini's header check is the loosest, so it goes last.
_CHAT_PARSERS = [chatgpt, claude_export, gemini]


class UnknownFormatError(Exception):
    pass


def parse_upload(filename: str, raw: bytes) -> ParseResult:
    """Parse an uploaded file (ZIP or bare JSON), auto-detecting the source."""
    if raw[:4] == b"PK\x03\x04":
        return _parse_zip(raw)
    return _parse_json_bytes(filename, raw)


def _parse_zip(raw: bytes) -> ParseResult:
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        candidates = [
            n for n in zf.namelist()
            if n.endswith(".json") and not n.startswith("__MACOSX")
        ]
        # Try likely files first: conversations.json (ChatGPT/Claude),
        # MyActivity.json (Gemini Takeout), then any remaining JSON.
        candidates.sort(
            key=lambda n: (
                0 if n.endswith("conversations.json")
                else 1 if n.endswith("MyActivity.json")
                else 2
            )
        )
        errors = []
        for name in candidates:
            try:
                return _parse_json_bytes(name, zf.read(name))
            except (UnknownFormatError, json.JSONDecodeError) as e:
                errors.append(f"{name}: {e}")
        html = [n for n in zf.namelist() if n.endswith("MyActivity.html")]
        if html:
            raise UnknownFormatError(
                "ZIP内にMyActivity.htmlしか見つかりません。TakeoutのエクスポートをJSON形式で"
                "やり直してください（[複数の形式] からJSONを選択）"
            )
        raise UnknownFormatError(
            "ZIP内に認識できるJSONがありません: " + "; ".join(errors[:5])
        )


def _parse_json_bytes(filename: str, raw: bytes) -> ParseResult:
    data = json.loads(raw)
    for parser in _CHAT_PARSERS:
        if parser.looks_like(data):
            return parser.parse(data)
    raise UnknownFormatError(
        f"{filename}: ChatGPT / Claude / Gemini のいずれの形式とも一致しません"
    )
