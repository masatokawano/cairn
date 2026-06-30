"""FixtureProvider — deterministic LLM stub for tests (Phase 3, P3-A).

Returns schema-valid JSON derived from a SHA-256 hash of the prompt, so
tests are reproducible without running a real LLM.

Failure injection: pass ``fail_first=N`` to simulate N consecutive
ValidationErrors before succeeding. Used to test the retry logic in
extract_with_validation().
"""
from __future__ import annotations

import hashlib
import json

from . import LLMProvider, ValidationError


class FixtureProvider(LLMProvider):
    """Deterministic provider for unit tests."""

    def __init__(self, fail_first: int = 0, responses: list[dict] | None = None) -> None:
        """
        fail_first: raise ValidationError this many times before returning.
        responses:  explicit response queue; pops from front, falls back to
                    hash-derived output when exhausted.
        """
        self._fail_first = fail_first
        self._calls = 0
        self._responses = list(responses or [])

    @property
    def name(self) -> str:
        return "fixture"

    @property
    def model(self) -> str | None:
        return "fixture-v1"

    def complete_structured(
        self,
        prompt: str,
        *,
        schema: dict,
        system: str | None = None,
        max_tokens: int = 2048,
        temperature: float = 0.0,
    ) -> dict:
        self._calls += 1
        if self._calls <= self._fail_first:
            raise ValidationError(f"fixture: injected failure #{self._calls}")

        if self._responses:
            return self._responses.pop(0)

        return _derive_from_schema(prompt, schema)

    def estimate_tokens(self, text: str) -> int:
        return max(1, len(text) // 4)

    @property
    def calls(self) -> int:
        return self._calls


def _derive_from_schema(prompt: str, schema: dict) -> dict:
    """Build a minimal valid object by filling required fields with
    deterministic values derived from SHA-256(prompt)."""
    digest = hashlib.sha256(prompt.encode()).hexdigest()
    required = schema.get("required", [])
    properties = schema.get("properties", {})
    result: dict = {}
    for i, key in enumerate(required):
        prop = properties.get(key, {})
        result[key] = _scalar(digest, i, prop)
    # fill non-required props with defaults too (keeps schema strict)
    for i, (key, prop) in enumerate(properties.items()):
        if key not in result:
            result[key] = _scalar(digest, i + 100, prop)
    return result


def _scalar(digest: str, idx: int, prop: dict):
    chunk = digest[idx * 2 % 60: idx * 2 % 60 + 4]
    ptype = prop.get("type", "string")
    enum = prop.get("enum")
    if enum:
        return enum[int(chunk, 16) % len(enum)]
    if ptype == "string":
        return f"fixture-{chunk}"
    if ptype == "integer":
        return int(chunk, 16) % 100
    if ptype == "number":
        return round(int(chunk, 16) / 65535, 3)
    if ptype == "boolean":
        return int(chunk, 16) % 2 == 0
    if ptype == "array":
        items_prop = prop.get("items", {"type": "string"})
        return [_scalar(digest, idx + 200, items_prop)]
    if ptype == "object":
        return {}
    return f"fixture-{chunk}"
