"""Personal Health Observatory (ADR-0005, DESIGN.md D13, docs/health/).

Independent health domain: an analytical store (DuckDB) plus raw source
snapshots under a protected data home OUTSIDE the git worktree — never
inside cairn.db. AGENTS.md invariant 9: no real health data in the
repository, fixtures, logs, commits or PRs; synthetic test data only.
"""
