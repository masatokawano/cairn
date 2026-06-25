"""Chunking for semantic search (Phase 2, P2-1a).

A message is one chunk by default. Long messages are split with a sliding
window so each chunk stays within an embedding model's typical context, while
overlap preserves a little continuity across the boundary. Each chunk records
its char offsets into the *original* message text, so the source span is always
recoverable (chunk.text == message.text[start_offset:end_offset]).

The algorithm is versioned via CURRENT_CHUNKING_VERSION: when it changes, bump
the string and re-run `admin rechunk --all`. Storing the version per chunk row
lets old and new chunks coexist during a staged re-generation.
"""
from __future__ import annotations

from dataclasses import dataclass

# Window width and overlap, in characters. 1500 chars ≈ 512 tokens for a
# multilingual model — a deliberate, retunable compromise (see phase2-design.md
# §3.3). The version string below encodes these so a change is auditable.
MAX_CHARS = 1500
OVERLAP = 200
CURRENT_CHUNKING_VERSION = "v1-char1500-overlap200"


@dataclass
class Chunk:
    idx: int           # order within the message (0-based)
    start_offset: int  # char offset into message.text, inclusive
    end_offset: int    # char offset into message.text, exclusive
    text: str          # == message.text[start_offset:end_offset]


def chunk_text(text: str) -> list[Chunk]:
    """Split one message's text into chunks.

    - Empty / whitespace-only text yields no chunks (nothing to embed).
    - Text within MAX_CHARS is a single chunk spanning the whole message.
    - Longer text uses a sliding window (width MAX_CHARS, overlap OVERLAP). The
      window end prefers a paragraph boundary (a blank line) in its second half,
      so splits land between paragraphs when one is available.

    Offsets always index the original `text`, so `text[c.start_offset:c.end_offset]`
    reproduces each chunk exactly (overlap means adjacent chunks share a span).
    """
    n = len(text)
    if n == 0 or not text.strip():
        return []
    if n <= MAX_CHARS:
        return [Chunk(idx=0, start_offset=0, end_offset=n, text=text)]

    chunks: list[Chunk] = []
    start = 0
    idx = 0
    while start < n:
        end = min(start + MAX_CHARS, n)
        if end < n:
            # Prefer a blank-line boundary in the window's second half. Searching
            # from start + MAX_CHARS//2 keeps chunks at least half-width, which
            # (being > OVERLAP) also guarantees forward progress below.
            boundary = text.rfind("\n\n", start + MAX_CHARS // 2, end)
            if boundary != -1:
                end = boundary + 2  # keep the blank line with the chunk that precedes it
        chunks.append(Chunk(idx=idx, start_offset=start, end_offset=end, text=text[start:end]))
        idx += 1
        if end >= n:
            break
        start = end - OVERLAP
    return chunks
