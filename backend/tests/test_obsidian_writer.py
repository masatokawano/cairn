"""deliver/obsidian_writer.py — the vault write allowlist (invariant 2).

Security-critical: these tests ENFORCE that writes land only in the three
allowlisted destinations and that traversal / absolute-path / symlink-escape
attempts are refused. A regression here is an invariant-2 violation.
"""
import importlib
import os
from pathlib import Path

import pytest


@pytest.fixture()
def vault(tmp_path, monkeypatch):
    root = tmp_path / "Obsidian"
    for sub in ("90 Auto", "40 Reviews/Weekly", "00 Inbox/AI Drafts",
                "10 Themes"):
        (root / "External Brain" / sub).mkdir(parents=True)
    monkeypatch.setenv("CAIRN_OBSIDIAN_VAULT", str(root))
    monkeypatch.delenv("CAIRN_EXTERNAL_BRAIN_DIR", raising=False)
    from app.deliver import obsidian_writer
    importlib.reload(obsidian_writer)
    return root, obsidian_writer


def eb(root: Path) -> Path:
    return root / "External Brain"


# --- happy paths --------------------------------------------------------------

def test_write_auto_overwrites(vault):
    root, w = vault
    p = w.write("auto", "cairn-recent.md", "本文1")
    assert p == eb(root) / "90 Auto" / "cairn-recent.md"
    assert p.read_text(encoding="utf-8") == "本文1"
    # overwrite allowed for 90 Auto
    p2 = w.write("auto", "cairn-recent.md", "本文2")
    assert p2.read_text(encoding="utf-8") == "本文2"


def test_write_weekly_new_only(vault):
    root, w = vault
    p = w.write("weekly", "2026-W27.md", "週次")
    assert p == eb(root) / "40 Reviews/Weekly" / "2026-W27.md"
    with pytest.raises(w.ObsidianWriteError, match="new-only"):
        w.write("weekly", "2026-W27.md", "上書き禁止")
    # original content untouched
    assert p.read_text(encoding="utf-8") == "週次"


def test_write_draft_new_only(vault):
    root, w = vault
    p = w.write("draft", "着想.md", "草案")
    assert p == eb(root) / "00 Inbox/AI Drafts" / "着想.md"
    with pytest.raises(w.ObsidianWriteError, match="new-only"):
        w.write("draft", "着想.md", "再")


def test_write_90_auto_helper(vault):
    root, w = vault
    paths = w.write_90_auto({"a.md": "A", "b.md": "B"})
    assert {p.name for p in paths} == {"a.md", "b.md"}
    assert (eb(root) / "90 Auto" / "a.md").read_text() == "A"


def test_creates_base_dir_if_missing(vault, tmp_path):
    root, w = vault
    # remove the AI Drafts dir; write must recreate it within the vault
    import shutil
    shutil.rmtree(eb(root) / "00 Inbox/AI Drafts")
    p = w.write("draft", "new.md", "x")
    assert p.exists()


# --- rejections: category & filename -----------------------------------------

def test_unknown_category_rejected(vault):
    _, w = vault
    with pytest.raises(w.ObsidianWriteError, match="unknown category"):
        w.write("30 Sources", "x.md", "nope")


def test_arbitrary_directory_not_writable(vault):
    _, w = vault
    # even a real vault dir that is NOT allowlisted must be unreachable
    for cat in ("themes", "10 Themes", "sources"):
        with pytest.raises(w.ObsidianWriteError):
            w.write(cat, "x.md", "nope")


@pytest.mark.parametrize("bad", [
    "../evil.md",
    "../../etc/passwd.md",
    "sub/dir.md",
    "a\\b.md",
    "/absolute/path.md",
    "..",
    ".",
    ".hidden.md",
    "noext",
    "trailing.txt",
    "with\x00null.md",
])
def test_unsafe_filenames_rejected(vault, bad):
    _, w = vault
    with pytest.raises(w.ObsidianWriteError):
        w.write("auto", bad, "nope")


def test_traversal_does_not_write_outside(vault, tmp_path):
    _, w = vault
    sentinel = tmp_path / "outside.md"
    with pytest.raises(w.ObsidianWriteError):
        w.write("auto", "../../../outside.md", "escaped")
    assert not sentinel.exists()


# --- rejections: symlink escape ----------------------------------------------

def test_symlinked_base_dir_escaping_vault_rejected(vault, tmp_path):
    root, w = vault
    outside = tmp_path / "outside_dir"
    outside.mkdir()
    # replace 90 Auto with a symlink pointing outside the vault
    auto = eb(root) / "90 Auto"
    import shutil
    shutil.rmtree(auto)
    auto.symlink_to(outside, target_is_directory=True)
    with pytest.raises(w.ObsidianWriteError, match="escapes the vault"):
        w.write("auto", "x.md", "escaped")
    assert not (outside / "x.md").exists()


def test_symlinked_intermediate_with_missing_subdir_creates_nothing(vault, tmp_path):
    """Codex M3 review blocker: if a middle component (External Brain) is a
    symlink out of the vault AND the allowlisted subdir does not yet exist,
    the write must be refused WITHOUT creating the subdir at the symlink's
    target (the pre-mkdir escape)."""
    root, w = vault
    outside = tmp_path / "outside_brain"
    outside.mkdir()
    # replace the whole External Brain dir with a symlink pointing outside,
    # and ensure 90 Auto does not exist under the target yet
    import shutil
    shutil.rmtree(eb(root))
    (root / "External Brain").symlink_to(outside, target_is_directory=True)
    assert not (outside / "90 Auto").exists()

    with pytest.raises(w.ObsidianWriteError, match="escapes the vault"):
        w.write("auto", "x.md", "escaped")

    # the crucial assertion: nothing was created at the symlink target
    assert not (outside / "90 Auto").exists()
    assert list(outside.iterdir()) == []


def test_symlinked_missing_subdir_itself_creates_nothing(vault, tmp_path):
    """A subdir-level symlink (90 Auto -> outside) that does not yet contain
    the expected tree must also be rejected without side effects."""
    root, w = vault
    outside = tmp_path / "outside_auto"
    outside.mkdir()
    import shutil
    shutil.rmtree(eb(root) / "90 Auto")
    (eb(root) / "90 Auto").symlink_to(outside, target_is_directory=True)
    with pytest.raises(w.ObsidianWriteError, match="escapes the vault"):
        w.write("auto", "x.md", "escaped")
    assert list(outside.iterdir()) == []


def test_symlinked_target_not_followed(vault, tmp_path):
    root, w = vault
    outside = tmp_path / "secret.md"
    # a planted symlink at the target must not be written through
    link = eb(root) / "90 Auto" / "cairn-recent.md"
    link.symlink_to(outside)
    with pytest.raises(w.ObsidianWriteError, match="symlink"):
        w.write("auto", "cairn-recent.md", "escaped")
    assert not outside.exists()


# --- atomicity ----------------------------------------------------------------

def test_no_tmp_left_behind_on_success(vault):
    root, w = vault
    w.write("auto", "x.md", "y")
    leftovers = list((eb(root) / "90 Auto").glob("*.tmp"))
    assert leftovers == []


def test_missing_vault_env_raises(vault, monkeypatch):
    _, w = vault
    monkeypatch.delenv("CAIRN_OBSIDIAN_VAULT")
    with pytest.raises(w.ObsidianWriteError, match="CAIRN_OBSIDIAN_VAULT"):
        w.write("auto", "x.md", "y")


def test_write_is_atomic_replace(vault, monkeypatch):
    """If the write fails mid-way, no partial file and no tmp remain."""
    root, w = vault
    w.write("auto", "keep.md", "original")

    import os as _os
    real_replace = _os.replace

    def boom(src, dst):
        raise OSError("simulated failure")

    monkeypatch.setattr(_os, "replace", boom)
    with pytest.raises(OSError):
        w.write("auto", "keep.md", "new content")
    monkeypatch.setattr(_os, "replace", real_replace)
    # original intact, no tmp litter
    assert (eb(root) / "90 Auto" / "keep.md").read_text() == "original"
    assert list((eb(root) / "90 Auto").glob("*.tmp")) == []
