"""LLMProvider abstraction (Phase 3, P3-A).

A provider turns a prompt + JSON schema into a validated dict. Concrete
providers live in sibling modules (ollama.py, fixture.py). The abstraction
mirrors EmbeddingProvider so callers stay model-agnostic.

Structured output is the only contract: every provider must accept a JSON
Schema dict and return output that validates against it. Free-text generation
is not part of this interface — Phase 3 only ever writes structured data to
the DB.
"""
from __future__ import annotations

from abc import ABC, abstractmethod


class LLMProvider(ABC):
    """Abstract LLM source for structured extraction."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Stable provider id stored in extraction_runs.provider (e.g. 'ollama')."""

    @property
    @abstractmethod
    def model(self) -> str | None:
        """Model id stored in extraction_runs.model (None for rules-based)."""

    @abstractmethod
    def complete_structured(
        self,
        prompt: str,
        *,
        schema: dict,
        system: str | None = None,
        max_tokens: int = 2048,
        temperature: float = 0.0,
    ) -> dict:
        """Return a dict validated against *schema*.

        Raises ``ValidationError`` if the provider returns output that does
        not match the schema after its internal retry (if any). Callers
        should treat this as a hard failure for one item.
        """

    @abstractmethod
    def estimate_tokens(self, text: str) -> int:
        """Rough token count for *text* (used for cost tracking, not billing)."""


class ValidationError(Exception):
    """Raised when LLM output fails JSON schema validation."""
