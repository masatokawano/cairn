"""Parser for Gemini history via Google Takeout "My Activity" (MyActivity.json).

Format (real Takeout, verified 2026-06-24): a JSON array of activity records
under header "Gemini アプリ" / "Gemini Apps".

  {header, title, time, products,
   subtitles?: [{name, url?}, ...],      # attached-file descriptions + meta lines
   imageFile?: "name.jpg",                # single attached image (legacy field)
   attachedFiles?: ["a.png", "b.jpg"],    # union of user uploads + assistant outputs
   safeHtmlItem?: [{html: "<p>...</p>"}], # assistant response, as HTML
   activityControls?, description?, locationInfos? }

Caveats (see NOTES.md):
- My Activity has NO thread structure — each record is a single prompt,
  so each becomes a one-or-two-message conversation here.
- The title carries the prompt with a locale-dependent prefix:
  "Prompted X" / "送信したメッセージ: X" / 「X と入力しました」 etc.
- Records starting with "フィードバックを送信しました" / "Sent feedback"
  are user-feedback log entries, not conversations — skipped.
- The assistant response lives in `safeHtmlItem[0].html` (HTML, with LaTeX
  inline). We strip tags to text. Older fixtures and some records put a
  short response in `subtitles` instead, but real exports also put
  attachment metadata there ("添付ファイル N 件", "画像を N 枚生成しました"),
  so subtitles are NOT treated as response text.
- Attachments: `<img src="...">` in safeHtmlItem points at assistant-
  generated images; the rest of imageFile / attachedFiles / subtitles{url}
  belong to the user. When the Takeout ZIP is passed via `attachments`,
  we hash + size the bytes; otherwise we record only `source_ref`.
"""
from __future__ import annotations

import hashlib
import re
from html import unescape as _html_unescape
from html.parser import HTMLParser

from .base import (
    ParseResult,
    ParsedAttachment,
    ParsedConversation,
    ParsedMessage,
    make_title,
)

SOURCE = "gemini"

# Tell the upload layer that gemini wants the surrounding ZIP's non-JSON
# entries handed in (for image hash/size); other parsers don't need this.
WANTS_ATTACHMENTS = True

_PROMPT_PREFIXES = (
    "Prompted ",
    "Asked ",
    "送信したメッセージ: ",
    "送信したメッセージ：",
)
_PROMPT_SUFFIXES = (" と入力しました", "と入力しました")
_FEEDBACK_PREFIXES = (
    "フィードバックを送信しました",
    "Sent feedback",
)

_IMG_RE = re.compile(r'<img[^>]*\bsrc="([^"]+)"', re.IGNORECASE)

# Best-effort MIME from filename extension.
_MIME_BY_EXT = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".heic": "image/heic",
    ".pdf": "application/pdf",
}


def looks_like(data) -> bool:
    return (
        isinstance(data, list)
        and len(data) > 0
        and isinstance(data[0], dict)
        and "header" in data[0]
        and "time" in data[0]
    )


def _strip_prompt_affixes(title: str) -> str:
    for p in _PROMPT_PREFIXES:
        if title.startswith(p):
            return title[len(p):].strip()
    for s in _PROMPT_SUFFIXES:
        if title.endswith(s):
            return title[: -len(s)].strip()
    return title.strip()


class _HTMLToText(HTMLParser):
    _BLOCK = {"p", "div", "li", "h1", "h2", "h3", "h4", "h5", "h6",
              "hr", "tr", "blockquote", "pre"}
    _SKIP = {"script", "style"}

    def __init__(self):
        super().__init__()
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip_depth += 1
        elif tag == "br":
            self._parts.append("\n")
        elif tag in self._BLOCK:
            self._parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skip_depth > 0:
            self._skip_depth -= 1
        elif tag in self._BLOCK:
            self._parts.append("\n")

    def handle_data(self, data):
        if self._skip_depth == 0:
            self._parts.append(data)


def _html_to_text(html: str) -> str:
    p = _HTMLToText()
    p.feed(html)
    raw = "".join(p._parts)
    raw = _html_unescape(raw)
    # collapse runs of blank lines, trim trailing whitespace per line
    lines = [ln.rstrip() for ln in raw.splitlines()]
    out: list[str] = []
    blank = 0
    for ln in lines:
        if not ln:
            blank += 1
            if blank <= 1:
                out.append("")
        else:
            blank = 0
            out.append(ln)
    return "\n".join(out).strip()


def _assistant_html(item: dict) -> str:
    shi = item.get("safeHtmlItem")
    if isinstance(shi, list) and shi and isinstance(shi[0], dict):
        h = shi[0].get("html")
        if isinstance(h, str):
            return h
    return ""


def _make_attachment(ref: str, attachments_map: dict | None) -> ParsedAttachment:
    """Look up bytes for `ref` in the Takeout ZIP if available.

    Takeout file entries arrive as full paths like
    "Takeout/My Activity/Gemini Apps/IMG_1245-....jpg" but the JSON refers
    to them by bare filename, and Takeout occasionally rewrites extensions
    (the JSON says `.jpeg` while the archived file is `.jpg`). We try exact
    match, then basename match, then a .jpg/.jpeg swap.
    """
    data: bytes | None = None
    if attachments_map:
        alt_refs = [ref]
        low = ref.lower()
        if low.endswith(".jpeg"):
            alt_refs.append(ref[:-5] + ".jpg")
        elif low.endswith(".jpg"):
            alt_refs.append(ref[:-4] + ".jpeg")
        for cand in alt_refs:
            data = attachments_map.get(cand)
            if data is not None:
                break
            for k, v in attachments_map.items():
                if k == cand or k.endswith("/" + cand) or k.endswith(cand):
                    data = v
                    break
            if data is not None:
                break
    mime = None
    lower = ref.lower()
    for ext, m in _MIME_BY_EXT.items():
        if lower.endswith(ext):
            mime = m
            break
    return ParsedAttachment(
        source_ref=ref,
        mime=mime,
        size=len(data) if data is not None else None,
        hash=hashlib.sha256(data).hexdigest() if data is not None else None,
        data=data,  # picked up by db.upsert_conversations → attachments.store()
    )


def _split_attachments(item: dict, attachments_map: dict | None
                       ) -> tuple[list[ParsedAttachment], list[ParsedAttachment]]:
    """Sort attachment references into (user_uploads, assistant_outputs).

    Assistant outputs are the <img src> references inside safeHtmlItem.
    Everything else (imageFile, attachedFiles, subtitles{url}) goes to the
    user — except references already claimed as assistant outputs."""
    html = _assistant_html(item)
    asst_refs: list[str] = []
    seen_asst: set[str] = set()
    for src in _IMG_RE.findall(html):
        if src and src not in seen_asst:
            asst_refs.append(src)
            seen_asst.add(src)

    user_refs: list[str] = []
    seen_user: set[str] = set()

    def _add_user(ref: str) -> None:
        if not isinstance(ref, str) or not ref:
            return
        if ref in seen_asst or ref in seen_user:
            return
        user_refs.append(ref)
        seen_user.add(ref)

    for s in item.get("subtitles") or []:
        if isinstance(s, dict):
            _add_user(s.get("url"))
    imgf = item.get("imageFile")
    if isinstance(imgf, str):
        _add_user(imgf)
    for f in item.get("attachedFiles") or []:
        if isinstance(f, str):
            _add_user(f)

    return ([_make_attachment(r, attachments_map) for r in user_refs],
            [_make_attachment(r, attachments_map) for r in asst_refs])


def parse(data, *, attachments: dict | None = None) -> ParseResult:
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
            if any(title_raw.startswith(p) for p in _FEEDBACK_PREFIXES):
                continue  # feedback log entry, not a conversation
            prompt = _strip_prompt_affixes(title_raw)
            user_atts, asst_atts = _split_attachments(item, attachments)
            # Skip records that carry no user-side content at all.
            if not prompt and not user_atts:
                continue
            time = item.get("time")
            messages = [
                ParsedMessage(
                    role="user",
                    text=prompt,
                    created_at=time,
                    attachments=user_atts,
                )
            ]
            asst_text = _html_to_text(_assistant_html(item))
            if asst_text or asst_atts:
                messages.append(
                    ParsedMessage(
                        role="assistant",
                        text=asst_text,
                        created_at=time,
                        attachments=asst_atts,
                    )
                )
            # Stable source_id: time + sha256(title_raw) keeps it content-derived
            # even when titles repeat, and is stable across re-imports.
            digest = hashlib.sha256(
                f"{time}|{title_raw}".encode("utf-8")
            ).hexdigest()[:16]
            title = make_title(prompt) if prompt else make_title(asst_text or "(添付のみ)")
            result.conversations.append(
                ParsedConversation(
                    source=SOURCE,
                    source_id=f"{time}-{digest}",
                    title=title,
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
