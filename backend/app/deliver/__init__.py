"""Write side of Cairn.

- `obsidian_writer.py` (M3) — the ONLY module that writes into the Obsidian
  vault, gated by an allowlist + path validation.
- `auto_lists.py` (M3) — generates the 90 Auto index markdown (content only,
  no I/O).
- `weekly_review.py` (M4) — weekly review generation (§5.4: activity +
  related-from-the-past + AI draft), written through obsidian_writer.

Invariant 2 (AGENTS.md): Obsidian writes are restricted to exactly three
paths — `90 Auto/` (overwrite), `40 Reviews/Weekly/` (new only), and
`00 Inbox/AI Drafts/` (new only) — enforced by allowlist + path validation
in obsidian_writer.py. Any addition to this module MUST NOT expand that set.
"""
