"""Secret redaction applied to message text at ingest time.

Patterns are based on gitleaks rules (loosened where vendors rotate key
formats). Replacement is `[REDACTED:<provider>]`; the original value must
never be stored, logged, or echoed in errors.

Order matters: anthropic (sk-ant-) must run before openai (sk-), and the
PEM block rule runs first because a key block could contain other tokens.
"""
from __future__ import annotations

import re

# (provider, compiled pattern) — applied in order.
PATTERNS: list[tuple[str, re.Pattern]] = [
    (
        "private-key",
        re.compile(
            r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY( BLOCK)?-----"
            r".*?"
            r"(-----END [A-Z0-9 ]*PRIVATE KEY( BLOCK)?-----|\Z)",
            re.DOTALL,
        ),
    ),
    ("anthropic", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}")),
    ("openai", re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}")),
    ("aws", re.compile(r"\b(?:A3T[A-Z0-9]|AKIA|ASIA|ABIA|ACCA)[A-Z0-9]{16}\b")),
    ("github", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}")),
    ("github", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,}")),
]

# Titles are truncated to ~60 chars, so a secret can be cut mid-token and
# escape the full-length patterns. For titles only, use shorter thresholds
# (false positives are acceptable there).
TITLE_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("anthropic", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{8,}")),
    ("openai", re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}")),
    ("aws", re.compile(r"\b(?:A3T[A-Z0-9]|AKIA|ASIA|ABIA|ACCA)[A-Z0-9]{8,}")),
    ("github", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}")),
    ("github", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{12,}")),
]


def redact(text: str) -> str:
    """Replace secrets with [REDACTED:provider]. Returns text unchanged if clean."""
    for provider, pattern in PATTERNS:
        text = pattern.sub(f"[REDACTED:{provider}]", text)
    return text


def redact_title(title: str) -> str:
    title = redact(title)
    for provider, pattern in TITLE_PATTERNS:
        title = pattern.sub(f"[REDACTED:{provider}]", title)
    return title


def scan(text: str) -> dict[str, int]:
    """Count matches per provider without modifying anything (for dry-run).

    Substitutes progressively on a working copy so overlapping patterns
    (anthropic sk-ant- vs openai sk-) aren't double-counted.
    """
    counts: dict[str, int] = {}
    for provider, pattern in PATTERNS:
        text, n = pattern.subn(f"[REDACTED:{provider}]", text)
        if n:
            counts[provider] = counts.get(provider, 0) + n
    return counts
