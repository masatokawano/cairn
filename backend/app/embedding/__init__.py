"""EmbeddingProvider abstraction (Phase 2, P2-1b).

A provider turns text into a fixed-size float32 vector. The two methods —
embed_passages and embed_query — exist as a pair because retrieval models
(notably the e5 family) want different prefixes on indexed text vs. queries;
hiding that distinction here keeps callers in the DB layer model-agnostic.

Vectors travel as `bytes` (f32 little-endian) so the DB BLOB column is the
single canonical form: pack on the way in, slice with the stored dimension
on the way out, and the same encoding works for every provider.
"""
from __future__ import annotations

import struct
from abc import ABC, abstractmethod


class EmbeddingProvider(ABC):
    """Abstract embedding source. Concrete providers live in sibling modules."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Stable provider id stored in embeddings.provider (e.g. 'local-sbert').
        Pair (name, model) is the addressable key for an embedding row."""

    @property
    @abstractmethod
    def model(self) -> str:
        """Model id stored in embeddings.model
        (e.g. 'intfloat/multilingual-e5-small')."""

    @property
    @abstractmethod
    def dimension(self) -> int:
        """Vector dimension. Must match every BLOB this provider produces."""

    @abstractmethod
    def embed_passages(self, texts: list[str]) -> list[bytes]:
        """Embed indexed text. One bytes object per input, f32 little-endian.
        For e5-style models the provider adds the 'passage: ' prefix itself."""

    @abstractmethod
    def embed_query(self, text: str) -> bytes:
        """Embed a query. Returns one f32 LE bytes object of `dimension` floats.
        The provider adds any model-specific query prefix internally."""


def vector_to_bytes(vec) -> bytes:
    """Pack a sequence of floats as f32 little-endian — the on-disk shape of
    embeddings.vector. Accepts any iterable of numbers (list, tuple, numpy)."""
    floats = [float(x) for x in vec]
    return struct.pack(f"<{len(floats)}f", *floats)


def bytes_to_vector(blob: bytes) -> list[float]:
    """Inverse of vector_to_bytes. Dimension is derived from len(blob) // 4 —
    the BLOB carries its own size, so callers don't need to thread it in."""
    n = len(blob) // 4
    return list(struct.unpack(f"<{n}f", blob))


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Pure-Python cosine. Used by db.find_similar_chunks until P2-1c installs
    a real vector index; numpy is intentionally not required here so the
    Phase-2 prototype runs with zero new deps in tests."""
    dot = na = nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / ((na ** 0.5) * (nb ** 0.5))
