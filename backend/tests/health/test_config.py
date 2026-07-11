"""H0 safety boundary: data home creation, permissions, rejections.

Maps to ACCEPTANCE.md H0: default home outside worktree / 0700+0600 /
worktree・traversal・symlink rejection.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from app.health import config


def test_default_home_is_outside_worktree_and_under_app_support(monkeypatch):
    monkeypatch.delenv(config.ENV_HOME, raising=False)
    home = config.resolve_home()
    assert "Library/Application Support/Cairn/health" in str(home)
    assert config._inside_git_worktree(home) is None


def test_ensure_home_creates_protected_tree(health_home):
    home = config.ensure_home()
    assert home == health_home.resolve()
    assert stat.S_IMODE(home.stat().st_mode) == 0o700
    for name in config.SUBDIRS:
        sub = home / name
        assert sub.is_dir()
        assert stat.S_IMODE(sub.stat().st_mode) == 0o700


def test_ensure_home_is_idempotent(health_home):
    first = config.ensure_home()
    second = config.ensure_home()
    assert first == second


def test_rejects_home_inside_git_worktree(monkeypatch):
    inside = Path(__file__).parent / "should-never-exist"
    monkeypatch.setenv(config.ENV_HOME, str(inside))
    with pytest.raises(config.HealthConfigError, match="worktree"):
        config.resolve_home()
    assert not inside.exists()


def test_rejects_traversal(monkeypatch, tmp_path):
    monkeypatch.setenv(config.ENV_HOME, str(tmp_path / "a" / ".." / "b"))
    with pytest.raises(config.HealthConfigError, match=r"\.\."):
        config.resolve_home()


def test_rejects_relative_path(monkeypatch):
    monkeypatch.setenv(config.ENV_HOME, "relative/health")
    with pytest.raises(config.HealthConfigError, match="absolute"):
        config.resolve_home()


def test_rejects_symlinked_home(monkeypatch, tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    monkeypatch.setenv(config.ENV_HOME, str(link))
    with pytest.raises(config.HealthConfigError, match="symlink"):
        config.resolve_home()


def test_rejects_symlinked_subdir(health_home, tmp_path):
    home = config.ensure_home()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    raw = home / "raw"
    raw.rmdir()
    raw.symlink_to(elsewhere)
    with pytest.raises(config.HealthConfigError, match="symlink"):
        config.ensure_home()


def test_protect_file_sets_0600(health_home):
    home = config.ensure_home()
    f = home / "store" / "x.bin"
    f.write_bytes(b"data")
    config.protect_file(f)
    assert stat.S_IMODE(f.stat().st_mode) == 0o600
