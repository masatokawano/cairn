"""Read-only clients for Karakeep, Zotero, and Obsidian.

Populated by M1 (karakeep, zotero) and M3 (obsidian). Kept empty at M0 so the
module skeleton in DESIGN.md §3 is present but no premature implementation
lands. All connectors here MUST be read-only against external systems
(invariant 1 in AGENTS.md); Cairn never writes back to Karakeep, Zotero, or
the original conversation stores.
"""
