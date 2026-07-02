"""URL and DOI normalisation for `items.url_norm` / `items.doi` (M1).

This module is a **placeholder** at M0. The full behaviour arrives in M1 per
DESIGN.md §5.2: scheme/host lowercasing, default-port stripping, trailing-slash
normalisation, fragment removal, tracking-parameter stripping (utm_*, fbclid,
gclid, si, t, ref_src, ...), domain aliases (twitter.com→x.com,
mobile.x.com→x.com, m.youtube.com→youtube.com, www. removal), GitHub repo
extraction from tree/blob URLs, and DOI prefix stripping + lowercasing.

Kept as a stub so the module tree in DESIGN.md §3 is in place, but exporting
no callable — importing this module in M0 code paths would be a scope
violation flagged in review.
"""
