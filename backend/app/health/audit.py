"""Repository audit for stray health artifacts (H0, PRIVACY.md §4).

Defense in depth behind the data-home placement and .gitignore: scans the
git worktree for filenames that look like real health data — tracked files
(the actual merge risk), stageable untracked files, and files present but
ignored (still a worktree presence PRIVACY.md §4 forbids). Pattern matching
is delegated to git pathspecs, so .venv/node_modules never get walked.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

# Keep in sync with the ADR-0005 block in .gitignore.
PATTERNS = (
    "export.xml",
    "export.zip",
    "apple_health_export*",
    "*.duckdb",
    "health*.csv",
    "*検査*.csv",          # Japanese lab-sheet exports (血液検査結果 …)
    "*visit-brief*.md",
    "lab-summary.md",
    "event-response-*.md",
    "*.parquet",
)


def _pathspecs() -> list[str]:
    return [f":(glob,icase)**/{p}" for p in PATTERNS]


def _git_files(repo_root: Path, *flags: str) -> list[str]:
    # core.quotepath=false keeps non-ASCII names (血液検査結果.csv) readable
    # instead of octal-escaped; -z gives NUL-separated, unquoted paths.
    out = subprocess.run(
        ["git", "-C", str(repo_root), "-c", "core.quotepath=false",
         "ls-files", "-z", *flags, "--", *_pathspecs()],
        capture_output=True, text=True, check=True,
    )
    return [line for line in out.stdout.split("\0") if line.strip()]


def find_repo_root(start: Path | None = None) -> Path | None:
    """Nearest ancestor with a .git entry; None when running outside a repo."""
    current = (start or Path(__file__)).resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def scan(repo_root: Path | None = None) -> dict:
    """Return {ok, tracked, untracked, ignored}.

    ``ok`` reflects only files that could reach a commit — tracked (already
    committed: the real merge risk) and untracked-but-stageable. A match in
    the *ignored* set is surfaced as a warning, not a failure: it cannot be
    committed, but PRIVACY.md §3 still wants real health data out of the
    worktree, so ``doctor`` nudges the user to relocate it.

    Synthetic fixtures are exempt only under tests/ paths containing
    'synthetic' in the filename — everything else that matches is flagged.
    """
    root = repo_root or find_repo_root()
    if root is None:
        return {"ok": True, "skipped": "not inside a git repository"}

    def keep(paths: list[str]) -> list[str]:
        return [p for p in paths
                if not ("tests/" in p and "synthetic" in Path(p).name.lower())]

    tracked = keep(_git_files(root, "--cached"))
    untracked = keep(_git_files(root, "--others", "--exclude-standard"))
    ignored = keep(_git_files(root, "--others", "--ignored", "--exclude-standard"))
    return {
        "ok": not (tracked or untracked),
        "tracked": tracked,
        "untracked": untracked,
        "ignored": ignored,
    }
