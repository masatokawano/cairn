"""core/keychain.py — secrets never leak into exceptions (invariant 5)."""
import subprocess

import pytest

from app.core import keychain


class FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_get_secret_success(monkeypatch):
    calls = {}

    def fake_run(cmd, **kwargs):
        calls["cmd"] = cmd
        return FakeProc(stdout="s3cr3t-value\n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert keychain.get_secret("brain-sync-karakeep") == "s3cr3t-value"
    assert calls["cmd"][0] == "security"
    assert "brain-sync-karakeep" in calls["cmd"]
    assert "-w" in calls["cmd"]


def test_get_secret_failure_message_has_no_stderr(monkeypatch):
    """`security` stderr must not be propagated — the message carries only
    the service name and exit code."""
    monkeypatch.setattr(
        subprocess, "run",
        lambda cmd, **kw: FakeProc(returncode=44, stderr="noise that might echo attributes"),
    )
    with pytest.raises(keychain.KeychainError) as exc_info:
        keychain.get_secret("brain-sync-zotero")
    msg = str(exc_info.value)
    assert "brain-sync-zotero" in msg
    assert "44" in msg
    assert "noise" not in msg


def test_get_secret_empty_result_raises(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: FakeProc(stdout="\n"))
    with pytest.raises(keychain.KeychainError):
        keychain.get_secret("brain-sync-karakeep")


def test_get_secret_oserror_wrapped(monkeypatch):
    def boom(cmd, **kw):
        raise FileNotFoundError("security not found")
    monkeypatch.setattr(subprocess, "run", boom)
    with pytest.raises(keychain.KeychainError):
        keychain.get_secret("brain-sync-karakeep")


def test_default_account_is_current_user(monkeypatch):
    calls = {}

    def fake_run(cmd, **kwargs):
        calls["cmd"] = cmd
        return FakeProc(stdout="x")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(keychain.getpass, "getuser", lambda: "masato")
    keychain.get_secret("svc")
    a_index = calls["cmd"].index("-a")
    assert calls["cmd"][a_index + 1] == "masato"
