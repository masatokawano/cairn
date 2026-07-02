from __future__ import annotations

import subprocess

import pytest

from brainsync import secrets


def test_get_secret_invokes_security(monkeypatch):
    recorded = {}

    def fake_run(cmd, **kwargs):
        recorded["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="sekrit\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    value = secrets.get_secret("brain-sync-karakeep", account="masato")

    assert value == "sekrit"
    assert recorded["cmd"] == [
        "security",
        "find-generic-password",
        "-a",
        "masato",
        "-s",
        "brain-sync-karakeep",
        "-w",
    ]


def test_get_secret_wraps_failure(monkeypatch):
    def fake_run(cmd, **kwargs):
        raise subprocess.CalledProcessError(44, cmd)

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(secrets.SecretError):
        secrets.get_secret("brain-sync-karakeep", account="masato")
