"""Parser registry and auto-detection for uploaded export files."""
from __future__ import annotations

import io
import json
import os
import re
import zipfile

from . import chatgpt, claude_export, gemini
from .base import PARSER_VERSION, ParseResult

__all__ = ["PARSER_VERSION", "ParseResult", "parse_upload",
           "UnknownFormatError", "FileTooLargeError"]

# Detection order matters: chatgpt's `mapping` and claude's `chat_messages`
# are unambiguous; gemini's header check is the loosest, so it goes last.
_CHAT_PARSERS = [chatgpt, claude_export, gemini]

# DoS guards for uploaded archives (see SECURITY.md #2).
MAX_JSON_BYTES = int(os.environ.get("CAIRN_MAX_JSON_MB", "500")) * 1024 * 1024
MAX_ZIP_ENTRIES = int(os.environ.get("CAIRN_MAX_ZIP_ENTRIES", "10000"))
# Per-attachment size when reading bytes from the surrounding ZIP for
# parsers that opt in via WANTS_ATTACHMENTS (currently: gemini Takeout).
MAX_ATTACHMENT_BYTES = int(os.environ.get("CAIRN_MAX_ATTACHMENT_MB", "50")) * 1024 * 1024


_CHATGPT_SHARD = re.compile(r"(^|/)conversations-\d+\.json$")


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


def _collect_zip_attachments(zf: zipfile.ZipFile, exclude: str) -> dict[str, bytes]:
    """Read non-JSON entries of `zf` into memory keyed by their archive name.

    Used by parsers that need to hash/size sibling files (currently gemini
    Takeout's images). Skips JSON, macOS metadata, and oversized files."""
    out: dict[str, bytes] = {}
    for n in zf.namelist():
        if (
            n == exclude
            or n.endswith(".json")
            or n.startswith("__MACOSX")
            or n.endswith("/")
        ):
            continue
        info = zf.getinfo(n)
        if info.file_size > MAX_ATTACHMENT_BYTES:
            continue
        with zf.open(n) as f:
            buf = f.read(MAX_ATTACHMENT_BYTES + 1)
        if len(buf) > MAX_ATTACHMENT_BYTES:
            continue  # ZIP header lied; drop quietly
        out[n] = buf
    return out


def _parse_zip(raw: bytes) -> ParseResult:
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        names = zf.namelist()
        if len(names) > MAX_ZIP_ENTRIES:
            raise FileTooLargeError(
                f"ZIP内のファイル数 ({len(names)}) が上限 ({MAX_ZIP_ENTRIES}) を超えています"
            )
        # Modern ChatGPT exports for large accounts split conversations across
        # conversations-000.json, conversations-001.json, ... — handle them
        # before the single-file fallback so we don't pick one and miss the
        # rest. Each shard has the same JSON shape (a flat list) as the
        # legacy single conversations.json.
        shards = sorted(
            n for n in names
            if _CHATGPT_SHARD.search(n) and not n.startswith("__MACOSX")
        )
        if shards:
            return _parse_chatgpt_shards(zf, shards)
        candidates = [
            n for n in names
            if n.endswith(".json") and not n.startswith("__MACOSX")
        ]
        # Try likely files first: conversations.json (ChatGPT/Claude),
        # MyActivity.json (Gemini Takeout), then any remaining JSON.
        # Takeout's localised filename varies (e.g. "マイアクティビティ.json"),
        # so also prefer anything under a "Gemini" subdir.
        def _priority(n: str) -> int:
            if n.endswith("conversations.json"):
                return 0
            if n.endswith("MyActivity.json"):
                return 1
            if "Gemini" in n:
                return 2
            return 3
        candidates.sort(key=_priority)
        errors = []
        for name in candidates:
            primary = _priority(name) <= 2
            try:
                return _parse_json_bytes(name, _read_bounded(zf, name), zf=zf)
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


def _parse_chatgpt_shards(zf: zipfile.ZipFile, shards: list[str]) -> ParseResult:
    """Parse a multi-file ChatGPT export and merge shards into one ParseResult.

    Each shard is parsed independently; a bad shard becomes a warning rather
    than failing the whole import, matching the parser-level tolerance the
    project uses elsewhere (NOTES: 壊れた行は warning にして skip)."""
    merged = ParseResult()
    for name in shards:
        try:
            data = json.loads(_read_bounded(zf, name))
        except FileTooLargeError as exc:
            merged.warnings.append(f"{name}: {exc}")
            continue
        except json.JSONDecodeError as exc:
            merged.warnings.append(f"{name}: invalid JSON ({exc})")
            continue
        if not chatgpt.looks_like(data):
            merged.warnings.append(
                f"{name}: ChatGPT 形式と一致しません (shard を skip)"
            )
            continue
        r = chatgpt.parse(data)
        merged.conversations.extend(r.conversations)
        merged.warnings.extend(r.warnings)
    return merged


def _parse_json_bytes(filename: str, raw: bytes,
                      *, zf: zipfile.ZipFile | None = None) -> ParseResult:
    data = json.loads(raw)
    for parser in _CHAT_PARSERS:
        if parser.looks_like(data):
            if zf is not None and getattr(parser, "WANTS_ATTACHMENTS", False):
                attachments = _collect_zip_attachments(zf, exclude=filename)
                return parser.parse(data, attachments=attachments)
            return parser.parse(data)
    raise UnknownFormatError(
        f"{filename}: ChatGPT / Claude / Gemini のいずれの形式とも一致しません"
    )
