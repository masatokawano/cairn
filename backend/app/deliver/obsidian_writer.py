"""Obsidian vault writer — the ONLY path that writes into the vault.

Invariant 2 (AGENTS.md / DESIGN.md §5.5): writes are restricted to exactly
three destinations, enforced here by an allowlist plus path validation. No
other module may write to the vault, and this set must never be widened
without a Decision Record change.

    category   directory                    policy
    --------   --------------------------   -----------------
    "auto"     90 Auto/                     overwrite allowed
    "weekly"   40 Reviews/Weekly/           new file only
    "draft"    00 Inbox/AI Drafts/          new file only

Threat model: the destination directories are constants and the vault root
comes from trusted config, but filenames (and content) can be derived from
untrusted external text (bookmark titles, note names, LLM output). So the
filename is the attack surface. Defenses, in depth:

  1. The filename must be a bare, safe name — no path separators, no NUL,
     not "." / "..", no leading dot, and it must end in ".md". This alone
     makes "../" traversal and absolute paths impossible.
  2. After building the target, the real (symlink-resolved) parent directory
     must equal the real allowlisted base directory. This catches a
     symlinked directory component that would otherwise escape the vault.
  3. We refuse to write *through* a symlink at the target itself (a planted
     symlink in 90 Auto could otherwise redirect an overwrite out of the
     vault). New-only destinations additionally refuse if anything already
     exists at the target.

Writes are a temp file in the destination dir + atomic os.replace, so a
crash never leaves a half-written note that Obsidian/iCloud would sync.

Content sanitization is the CALLER's job (see deliver/auto_lists.py, which
collapses untrusted text to a single line). This module is about *where* a
write may land, not *what* it contains.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

# category -> (subdirectory under the External Brain folder, overwrite_allowed)
ALLOWLIST: dict[str, tuple[str, bool]] = {
    "auto": ("90 Auto", True),
    "weekly": ("40 Reviews/Weekly", False),
    "draft": ("00 Inbox/AI Drafts", False),
}


class ObsidianWriteError(RuntimeError):
    """A write was refused (bad category, unsafe filename, escape attempt,
    or a new-only destination that already exists)."""


def _vault_root() -> Path:
    vault = os.environ.get("CAIRN_OBSIDIAN_VAULT")
    if not vault:
        raise ObsidianWriteError("CAIRN_OBSIDIAN_VAULT is not set")
    root = Path(vault).expanduser()
    if not root.is_dir():
        raise ObsidianWriteError(f"vault not found: {root}")
    # strict=True: fail loudly rather than write to a non-existent target
    return root.resolve(strict=True)


def _validate_filename(filename: str) -> None:
    """Reject anything that isn't a bare, safe ``*.md`` name."""
    if not filename or filename in (".", ".."):
        raise ObsidianWriteError(f"unsafe filename: {filename!r}")
    if filename.startswith("."):
        raise ObsidianWriteError(f"refusing dotfile: {filename!r}")
    if "\x00" in filename or "/" in filename or "\\" in filename:
        raise ObsidianWriteError(f"filename must not contain path separators: {filename!r}")
    # os.sep / os.altsep belt-and-suspenders (covers exotic platforms)
    if os.sep in filename or (os.altsep and os.altsep in filename):
        raise ObsidianWriteError(f"filename must not contain path separators: {filename!r}")
    if not filename.endswith(".md"):
        raise ObsidianWriteError(f"filename must end in .md: {filename!r}")


def _resolve_target(category: str, filename: str) -> tuple[Path, Path]:
    """Validate inputs and return (target_path, base_real_dir). Creates the
    allowlisted base directory if missing. Raises ObsidianWriteError on any
    policy violation. Does NOT write."""
    if category not in ALLOWLIST:
        raise ObsidianWriteError(
            f"unknown category {category!r}; allowed: {sorted(ALLOWLIST)}"
        )
    _validate_filename(filename)
    subdir, overwrite_allowed = ALLOWLIST[category]

    root = _vault_root()
    brain_dir = os.environ.get("CAIRN_EXTERNAL_BRAIN_DIR", "External Brain")
    base = root / brain_dir / subdir
    base.mkdir(parents=True, exist_ok=True)
    base_real = base.resolve(strict=True)

    # The allowlisted base must itself stay within the vault (guards a
    # symlinked External Brain / subdir pointing outside).
    if not base_real.is_relative_to(root):
        raise ObsidianWriteError(f"destination escapes the vault: {base_real}")

    target = base / filename
    # filename has no separators, so target.parent is base; resolving it
    # catches a symlinked component between vault root and base.
    if target.parent.resolve(strict=True) != base_real:
        raise ObsidianWriteError(f"destination escapes the vault: {target}")

    if target.is_symlink():
        raise ObsidianWriteError(f"refusing to write through a symlink: {target}")
    if target.exists():
        if target.is_dir():
            raise ObsidianWriteError(f"target is a directory: {target}")
        if not overwrite_allowed:
            raise ObsidianWriteError(
                f"{category!r} is new-only and {filename!r} already exists"
            )
    return target, base_real


def write(category: str, filename: str, content: str) -> Path:
    """Write ``content`` to ``filename`` in the allowlisted ``category``
    directory. Returns the written path. Raises ObsidianWriteError if the
    destination is not allowed. Atomic: temp file + os.replace."""
    target, base_real = _resolve_target(category, filename)

    fd, tmp_name = tempfile.mkstemp(dir=base_real, suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        # Normal note permissions (mkstemp defaults to 0600, which is odd for
        # a user-managed vault synced by Obsidian/iCloud).
        os.chmod(tmp, 0o644)
        os.replace(tmp, target)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    return target


def write_90_auto(lists: dict[str, str]) -> list[Path]:
    """Write the 90 Auto index files (see deliver/auto_lists.generate_all).
    Overwrite is allowed for this destination. Returns the written paths."""
    return [write("auto", name, content) for name, content in lists.items()]
