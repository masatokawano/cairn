"""Tests for `admin audit-deps` — pip-audit wrapper.

The actual pip-audit invocation is mocked: we don't want CI to fail because
PyPI advisory data drifted, and `uvx pip-audit` makes a network call we'd
rather not bake into the suite. The contract under test is:

- the constructed argv matches the documented shape
  (`--no-deps --disable-pip` against requirements.lock)
- the exit code from pip-audit is propagated verbatim (so CI can gate on it)
- missing `uvx` surfaces an actionable error, not a stack trace
"""
import subprocess

import pytest

from app import admin


def test_audit_deps_argv_targets_lockfile_with_no_deps_flags(monkeypatch):
    captured = {}

    def fake_run(argv, check=False):
        captured["argv"] = argv
        captured["check"] = check
        return subprocess.CompletedProcess(argv, returncode=0)

    monkeypatch.setattr(admin.subprocess, "run", fake_run)
    rc = admin.cmd_audit_deps(None)
    assert rc == 0
    argv = captured["argv"]
    # documented invocation: don't recreate the inner venv (SIGABRT on macOS)
    assert argv[0] == "uvx"
    assert "pip-audit" in argv
    assert "--no-deps" in argv and "--disable-pip" in argv
    # the lockfile is the audit target — pinned versions, no resolution
    lock_idx = argv.index("-r") + 1
    assert argv[lock_idx].endswith("requirements.lock")
    # never run with check=True: that converts pip-audit's exit 1 into a
    # CalledProcessError and we'd lose the exit code we want to propagate
    assert captured["check"] is False


def test_audit_deps_propagates_pip_audit_exit_code(monkeypatch):
    """CI gates on the exit code (0 clean, 1+ findings). The wrapper must
    forward whatever pip-audit returned — masking it would break gating."""
    monkeypatch.setattr(
        admin.subprocess, "run",
        lambda argv, check=False: subprocess.CompletedProcess(argv, returncode=1),
    )
    assert admin.cmd_audit_deps(None) == 1


def test_audit_deps_missing_uvx_returns_127_with_hint(monkeypatch, capsys):
    def fake_run(*_a, **_k):
        raise FileNotFoundError(2, "No such file or directory: 'uvx'")

    monkeypatch.setattr(admin.subprocess, "run", fake_run)
    rc = admin.cmd_audit_deps(None)
    assert rc == 127
    err = capsys.readouterr().err
    # the hint must mention uv and the direct fallback command
    assert "uv" in err
    assert "pip-audit" in err
