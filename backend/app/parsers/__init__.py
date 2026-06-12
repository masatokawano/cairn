"""Parser registry and auto-detection for uploaded export files."""
from __future__ import annotations

import io
import json
import os
import zipfile

from . import chatgpt, claude_export, gemini
from .base import ParseResult

# Detection order matters: chatgpt's `mapping` and claude's `chat_messages`
# are unambiguous; gemini's header check is the loosest, so it goes last.
_CHAT_PARSERS = [chatgpt, claude_export, gemini]

# DoS guards for uploaded archives (see SECURITY.md #2).
MAX_JSON_BYTES = int(os.environ.get("CAIRN_MAX_JSON_MB", "500")) * 1024 * 1024
MAX_ZIP_ENTRIES = int(os.environ.get("CAIRN_MAX_ZIP_ENTRIES", "10000"))


class UnknownFormatError(Exception):
    pass


class FileTooLargeError(Exception):
    pass


def parse_upload(filename: str, raw: bytes) -> ParseResult:
    """Parse an uploaded file (ZIP or bare JSON), auto-detecting the source."""
    if raw[:4] == b"PK\x03\x04":
        return _parse_zip(raw)
    return _parse_json_bytes(filename, raw)


def _read_bounded(zf: zipfile.ZipFile, name: str) -> bytes:
    """Decompress at most MAX_JSON_BYTES; the header's file_size can lie,
    so enforce the limit on actual bytes read (zip-bomb guard)."""
    with zf.open(name) as f:
        data = f.read(MAX_JSON_BYTES + 1)
    if len(data) > MAX_JSON_BYTES:
        raise FileTooLargeError(
            f"{name} の展開サイズが上限 ({MAX_JSON_BYTES // (1024 * 1024)}MB) を超えています"
        )
    return data


def _parse_zip(raw: bytes) -> ParseResult:
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        names = zf.namelist()
        if len(names) > MAX_ZIP_ENTRIES:
            raise FileTooLargeError(
                f"ZIP内のファイル数 ({len(names)}) が上限 ({MAX_ZIP_ENTRIES}) を超えています"
            )
        candidates = [
            n for n in names
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
            primary = name.endswith(("conversations.json", "MyActivity.json"))
            try:
                return _parse_json_bytes(name, _read_bounded(zf, name))
            except FileTooLargeError:
                if primary:
                    raise  # the file the user actually needs is too big — surface it
                errors.append(f"{name}: too large, skipped")
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
