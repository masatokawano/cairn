"""Write side: Obsidian writer (allowlisted) and weekly-review renderer.

Populated by M3 (obsidian_writer) and M4 (weekly_review). Kept empty at M0.

Invariant 2 (AGENTS.md): Obsidian writes are restricted to exactly three
paths — `90 Auto/` (overwrite), `40 Reviews/Weekly/` (new only), and
`00 Inbox/AI Drafts/` (new only) — enforced by allowlist + path validation
in obsidian_writer.py. Any addition to this module MUST NOT expand that set.
"""
