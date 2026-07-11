"""Health data home resolution and protection (H0, docs/health/PRIVACY.md §3).

The data home defaults to ``~/Library/Application Support/Cairn/health/``
(outside every TCC-protected directory and outside the git worktree) and can
be overridden with ``CAIRN_HEALTH_HOME`` for tests and advanced setups.

Enforced here, before anything touches disk:

- the home must be an absolute path and must NOT live inside a git worktree
  (a health.duckdb or raw export committed by accident is the worst-case
  failure mode — PRIVACY.md §4);
- the home and its subdirectories must not be symlinks (a planted symlink
  could redirect raw snapshots outside the protected tree);
- directories are chmod 0700 and data files 0600 (FileVault protects the
  disk, POSIX modes protect against other local users/processes).
"""
from __future__ import annotations

import os
from pathlib import Path

ENV_HOME = "CAIRN_HEALTH_HOME"
DEFAULT_HOME = "~/Library/Application Support/Cairn/health"

SUBDIRS = ("raw", "store", "derived", "reports", "quarantine", "backups")

DIR_MODE = 0o700
FILE_MODE = 0o600


class HealthConfigError(Exception):
    """Unsafe or invalid health data home configuration."""


def _inside_git_worktree(path: Path) -> Path | None:
    """Return the worktree root containing ``path``, or None.

    A ``.git`` entry may be a directory (normal clone) or a file (linked
    worktree) — both mark a worktree root.
    """
    for candidate in (path, *path.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def resolve_home() -> Path:
    """Validate and return the health data home path. Does NOT create it."""
    raw = os.environ.get(ENV_HOME) or DEFAULT_HOME
    expanded = Path(os.path.expanduser(raw))
    if not expanded.is_absolute():
        raise HealthConfigError(f"health home must be an absolute path: {raw!r}")
    if ".." in expanded.parts:
        raise HealthConfigError(f"health home must not contain '..': {raw!r}")
    if expanded.is_symlink():  # catches dangling symlinks too
        raise HealthConfigError(f"health home must not be a symlink: {expanded}")

    # Resolve symlinked ancestors, then re-check containment on the REAL path
    # so a symlink cannot smuggle the home into a worktree.
    resolved = expanded.resolve()
    worktree = _inside_git_worktree(resolved)
    if worktree is not None:
        raise HealthConfigError(
            f"health home {resolved} is inside the git worktree {worktree}; "
            "real health data must never live in the repository "
            "(PRIVACY.md §3, AGENTS.md invariant 9)"
        )
    return resolved


def ensure_home() -> Path:
    """Create (idempotently) the protected data home and its subdirectories."""
    home = resolve_home()
    home.mkdir(parents=True, exist_ok=True)
    os.chmod(home, DIR_MODE)
    for name in SUBDIRS:
        sub = home / name
        if sub.is_symlink():
            raise HealthConfigError(f"refusing symlinked subdirectory: {sub}")
        sub.mkdir(exist_ok=True)
        os.chmod(sub, DIR_MODE)
    return home


def protect_file(path: Path) -> None:
    """chmod a data file to 0600 (create-then-protect pattern)."""
    os.chmod(path, FILE_MODE)


def store_path(home: Path) -> Path:
    return home / "store" / "health.duckdb"
