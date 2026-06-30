"""GitHub repository detector (P3-B).

Extracts `github.com/owner/repo` patterns from message text.  Returns
EntityMatch instances with kind="repo" and external_id="owner/repo".

We deliberately do NOT rely on the URL detector for this: the URL detector
normalises to the domain level, while repos need the owner/repo path.
Both detectors can match the same text; duplicates at the DB level are
prevented by UNIQUE(entity_id, message_id, start_offset).
"""
from __future__ import annotations

import re
from .urls import EntityMatch

DETECTOR = "rules-repo-v1"

# Matches github.com/owner/repo (greedy; we post-process to strip .git and
# trailing punctuation from the repo segment).
_REPO_RE = re.compile(
    r'(?:https?://)?github\.com/'
    r'([\w.\-]{1,100})/([\w.\-]{1,100})',
    re.IGNORECASE,
)

# Strip these suffixes from the repo name component.
_REPO_TRAIL = re.compile(r'(?:\.git|[.,;:!?)\]"\'。，．]+)$')

# Don't treat these as repos (GitHub built-in paths).
_SKIP_OWNERS = frozenset({
    "orgs", "teams", "apps", "marketplace", "sponsors", "features",
    "pricing", "contact", "about", "login", "logout", "join",
    "settings", "notifications", "explore", "trending", "topics",
    "collections", "events", "pulls", "issues", "search",
})


def extract_repos(text: str) -> list[EntityMatch]:
    """Return all GitHub repo matches found in *text*."""
    matches: list[EntityMatch] = []
    for m in _REPO_RE.finditer(text):
        owner = m.group(1).lower()
        raw_repo = m.group(2)
        # Strip .git and trailing punctuation from the repo component.
        repo = _REPO_TRAIL.sub("", raw_repo).lower()
        if not repo:
            continue
        if owner in _SKIP_OWNERS:
            continue
        # Strip common non-repo sub-path names.
        if repo in ("blob", "tree", "commit", "pull", "issues", "wiki",
                    "actions", "releases", "tags", "branches", "compare"):
            continue
        external_id = f"{owner}/{repo}"
        canonical = f"https://github.com/{external_id}"
        # surface only covers the cleaned portion
        surface = f"github.com/{owner}/{raw_repo}" if not m.group(0).startswith("http") \
            else f"https://github.com/{owner}/{raw_repo}"
        end = m.start() + len(m.group(0)) - (len(raw_repo) - len(_REPO_TRAIL.sub("", raw_repo)))
        matches.append(EntityMatch(
            kind="repo",
            canonical_name=canonical,
            external_id=external_id,
            surface=surface,
            start=m.start(),
            end=end,
            detector=DETECTOR,
        ))
    return matches
