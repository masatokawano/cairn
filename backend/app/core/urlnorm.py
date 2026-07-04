"""URL and DOI normalisation for `items.url_norm` / `items.doi` (M1, DESIGN.md §5.2).

This module decides the cross-source join rate (S5): a bookmark saved in
Karakeep, a paper in Zotero, and a URL pasted into a conversation must all
normalise to the same key before item_links can connect them. Pure functions,
no DB access.

Normalisation steps (in order):
- reject non-http(s) schemes, drop userinfo (never keep credentials in a key)
- lowercase scheme/host, strip a trailing host dot, strip default ports
- strip a leading ``www.``, then apply HOST_ALIASES (twitter.com→x.com etc.)
- site rewrites: youtu.be/<id> → youtube.com/watch?v=<id>,
  arxiv.org/{pdf,html}/<id>[vN][.pdf] → arxiv.org/abs/<id> (version dropped:
  the paper, not the revision, is the identity for linking)
- drop tracking query params (utm_*, fbclid, gclid, si, t, …; x.com also
  drops ``s``), sort the survivors for a stable key, drop the fragment,
  strip trailing slashes

The tracking-param list is code-managed below and extendable without a code
change via ``CAIRN_TRACKING_PARAMS`` (comma-separated names).
"""
from __future__ import annotations

import os
import re
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

# Tracking / share-noise params stripped from every URL. ``t`` is stripped
# globally: on x.com it is share noise and on YouTube a timestamp — either
# way the *page* identity is the same, which is what linking needs.
TRACKING_PARAMS = {
    "fbclid", "gclid", "dclid", "msclkid", "igshid", "si", "t",
    "ref_src", "ref_url", "mc_cid", "mc_eid",
}
TRACKING_PREFIXES = ("utm_",)

# Params that are tracking noise only on specific hosts (applied after host
# normalisation). ``s`` is a share tag on x.com but a search query elsewhere.
HOST_TRACKING_PARAMS = {
    "x.com": {"s"},
}

HOST_ALIASES = {
    "twitter.com": "x.com",
    "mobile.twitter.com": "x.com",
    "m.twitter.com": "x.com",
    "mobile.x.com": "x.com",
    "m.youtube.com": "youtube.com",
}

_DEFAULT_PORTS = {"http": "80", "https": "443"}

# First path segments on github.com that are site pages, not repo owners.
_GITHUB_NON_OWNERS = {
    "about", "apps", "codespaces", "collections", "contact", "customer-stories",
    "enterprise", "explore", "features", "join", "login", "marketplace", "new",
    "notifications", "orgs", "pricing", "search", "settings", "sponsors",
    "topics", "trending",
}

# RFC 3986 charset: stops cleanly at CJK prose around a pasted URL. Raw
# (unencoded) IRI paths get truncated at the first non-ASCII char — the
# percent-encoded form is what normalisation keys on anyway.
_URL_RE = re.compile(r"https?://[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+", re.IGNORECASE)
_TRAILING_JUNK = ".,;:!?'\""
_ARXIV_PATH_RE = re.compile(r"^/(abs|pdf|html)/(.+?)(?:\.pdf)?$", re.IGNORECASE)
_ARXIV_VERSION_RE = re.compile(r"v\d+$")


def _tracking_params() -> set[str]:
    extra = os.environ.get("CAIRN_TRACKING_PARAMS", "")
    return TRACKING_PARAMS | {p.strip().lower() for p in extra.split(",") if p.strip()}


def _norm_host(host: str) -> str:
    host = host.lower().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    return HOST_ALIASES.get(host, host)


def normalize_url(url: str | None) -> str | None:
    """Return the canonical form of an http(s) URL, or None if not one."""
    if not url:
        return None
    url = url.strip()
    # urlsplit is lazy: .hostname/.port raise ValueError only on access, e.g.
    # for shell snippets in conversation text like http://localhost:$PORT/x —
    # so port/hostname access must sit inside this try, not just urlsplit().
    try:
        parts = urlsplit(url)
        scheme = parts.scheme.lower()
        if scheme not in ("http", "https") or not parts.hostname:
            return None
        host = _norm_host(parts.hostname)
        port = parts.port
    except ValueError:
        return None
    netloc = host if port is None or str(port) == _DEFAULT_PORTS[scheme] else f"{host}:{port}"

    path = parts.path
    query_pairs = parse_qsl(parts.query, keep_blank_values=True)

    if host == "youtu.be":
        video_id = path.strip("/").split("/")[0]
        if video_id:
            host = netloc = "youtube.com"
            path = "/watch"
            query_pairs = [("v", video_id)] + query_pairs
    elif host == "arxiv.org":
        m = _ARXIV_PATH_RE.match(path)
        if m:
            paper_id = _ARXIV_VERSION_RE.sub("", m.group(2))
            path = f"/abs/{paper_id}"

    drop = _tracking_params() | HOST_TRACKING_PARAMS.get(host, set())
    kept = sorted(
        (k, v) for k, v in query_pairs
        if k.lower() not in drop and not k.lower().startswith(TRACKING_PREFIXES)
    )
    query = urlencode(kept)
    path = path.rstrip("/")
    return urlunsplit((scheme, netloc, path, query, ""))


def extract_github_repo(url: str | None) -> str | None:
    """Return a repo-level key ``github.com/{owner}/{repo}`` or None.

    Deep paths (tree/blob/issues/…) collapse to the repo; owner/repo are
    lowercased (GitHub is case-insensitive there) and ``.git`` is stripped.
    """
    norm = normalize_url(url)
    if not norm:
        return None
    parts = urlsplit(norm)
    if parts.hostname != "github.com":
        return None
    segments = [s for s in parts.path.split("/") if s]
    if len(segments) < 2 or segments[0].lower() in _GITHUB_NON_OWNERS:
        return None
    owner, repo = segments[0].lower(), segments[1].lower()
    repo = repo.removesuffix(".git")
    if not repo:
        return None
    return f"github.com/{owner}/{repo}"


def normalize_doi(value: str | None) -> str | None:
    """Normalise a DOI given as ``10.x/…``, ``doi:10.x/…``, or a doi.org URL."""
    if not value:
        return None
    doi = value.strip()
    m = re.match(r"^https?://(?:dx\.)?doi\.org/(.+)$", doi, re.IGNORECASE)
    if m:
        doi = unquote(m.group(1))
    if doi.lower().startswith("doi:"):
        doi = doi[4:].strip()
    doi = doi.lower().rstrip("/")
    if not doi.startswith("10.") or "/" not in doi:
        return None
    return doi


def extract_urls(text: str) -> list[str]:
    """Extract http(s) URLs from free text (conversation bodies), deduped,
    in order of first appearance. Trailing punctuation and unbalanced closing
    brackets (markdown ``[x](url)`` artifacts) are trimmed."""
    seen: dict[str, None] = {}
    for match in _URL_RE.finditer(text):
        url = match.group(0)
        while url:
            last = url[-1]
            if last in _TRAILING_JUNK:
                url = url[:-1]
            elif last == ")" and url.count("(") < url.count(")"):
                url = url[:-1]
            elif last == "]" and url.count("[") < url.count("]"):
                url = url[:-1]
            else:
                break
        if url:
            seen.setdefault(url)
    return list(seen)


def url_keys(url: str | None) -> tuple[str | None, str | None, str | None]:
    """All three linkage keys for one raw URL: (url_norm, doi, github_repo)."""
    norm = normalize_url(url)
    if norm is None:
        return None, None, None
    return norm, normalize_doi(norm), extract_github_repo(norm)
