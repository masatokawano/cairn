"""URL detector (P3-B).

Extracts HTTP/HTTPS URLs from message text, normalises them, and returns
EntityMatch instances ready for db.upsert_entity / upsert_entity_mention.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse, urlunparse, urlencode, parse_qsl

DETECTOR = "rules-url-v1"

# Matches http(s) URLs.  Stops at whitespace and common sentence-terminating
# chars that are unlikely to be part of a URL (>,), "', <>).
_URL_RE = re.compile(
    r'https?://'
    r'[^\s<>"\')\]]+',
    re.IGNORECASE,
)

# UTM and tracking params to strip.
_STRIP_PARAMS = frozenset({
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "ref", "referrer", "source",
})

# Punctuation that commonly trails a URL in prose but is not part of it.
_TRAIL = re.compile(r'[.,;:!?)\]>"」。，．]+$')


@dataclass
class EntityMatch:
    kind: str
    canonical_name: str
    external_id: str | None
    surface: str
    start: int
    end: int
    detector: str


def extract_urls(text: str) -> list[EntityMatch]:
    """Return all URL matches found in *text*."""
    matches: list[EntityMatch] = []
    for m in _URL_RE.finditer(text):
        raw = m.group(0)
        # Strip trailing punctuation that likely belongs to the prose.
        stripped = _TRAIL.sub("", raw)
        if not stripped:
            continue
        canonical = _normalise_url(stripped)
        if canonical is None:
            continue
        parsed = urlparse(canonical)
        external_id = parsed.netloc.lower() or None
        end = m.start() + len(stripped)
        matches.append(EntityMatch(
            kind="url",
            canonical_name=canonical,
            external_id=external_id,
            surface=stripped,
            start=m.start(),
            end=end,
            detector=DETECTOR,
        ))
    return matches


def _normalise_url(url: str) -> str | None:
    """Normalise URL: lowercase scheme+host, strip tracking params, strip trailing slash."""
    try:
        p = urlparse(url)
    except Exception:
        return None
    if p.scheme not in ("http", "https"):
        return None
    netloc = p.netloc.lower()
    # Strip tracking params from query string.
    qs_pairs = [(k, v) for k, v in parse_qsl(p.query) if k.lower() not in _STRIP_PARAMS]
    query = urlencode(qs_pairs) if qs_pairs else ""
    path = p.path.rstrip("/") or ""
    canonical = urlunparse((p.scheme, netloc, path, "", query, ""))
    return canonical if canonical else None
