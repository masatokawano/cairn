"""Tests for M6 unattended-run failure notification (app/ops.py + cli.main).

Covers S4: an agent run that exits non-zero must surface (log line + one
macOS notification), but only when launched by an alerting agent
(CAIRN_NOTIFY), keyed on the exit code (not the legitimately-noisy stderr),
and never masking the real exit code.
"""
import pytest


@pytest.fixture()
def ops(tmp_path, monkeypatch):
    monkeypatch.setenv("CAIRN_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.delenv("CAIRN_AGENT", raising=False)
    from app import ops as ops_module
    return ops_module


def test_should_notify_reads_env(ops, monkeypatch):
    monkeypatch.delenv("CAIRN_NOTIFY", raising=False)
    assert ops.should_notify() is False
    for truthy in ("1", "true", "YES", "on"):
        monkeypatch.setenv("CAIRN_NOTIFY", truthy)
        assert ops.should_notify() is True
    for falsy in ("0", "", "no"):
        monkeypatch.setenv("CAIRN_NOTIFY", falsy)
        assert ops.should_notify() is False


def test_notify_failure_logs_and_calls_notifier(ops, tmp_path):
    calls = []
    rec = ops.notify_failure(1, agent="sync",
                             notifier=lambda t, m: calls.append((t, m)))
    log = (tmp_path / "logs" / "failures.log").read_text()
    assert "\tsync\texit=1" in log
    assert len(calls) == 1
    title, message = calls[0]
    assert "sync" in title
    assert "exit 1" in message and "sync-error.log" in message
    assert rec["code"] == 1 and rec["agent"] == "sync"


def test_notify_failure_default_agent_from_env(ops, monkeypatch):
    monkeypatch.setenv("CAIRN_AGENT", "weekly")
    seen = []
    ops.notify_failure(2, notifier=lambda t, m: seen.append(t))
    assert "weekly" in seen[0]


def test_notify_failure_swallows_notifier_error(ops):
    def boom(t, m):
        raise RuntimeError("no osascript here")

    # A broken notifier must not raise — the run already failed.
    ops.notify_failure(3, agent="sync", notifier=boom)


def test_notify_failure_survives_unwritable_log_dir(ops, monkeypatch, tmp_path):
    # point at a path whose parent is a file → mkdir fails; must not raise
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    monkeypatch.setenv("CAIRN_LOG_DIR", str(blocker / "logs"))
    calls = []
    ops.notify_failure(1, agent="sync", notifier=lambda t, m: calls.append(m))
    assert len(calls) == 1  # notification still fires even if the log can't


def test_applescript_literal_escapes_untrusted_text(ops):
    # backslashes and quotes in an error message must not break out of the
    # AppleScript string literal.
    assert ops._as('a"b\\c') == '"a\\"b\\\\c"'


# --- cli.main() wiring -------------------------------------------------------

def _raiser(code):
    def run():
        raise SystemExit(code)
    return run


def test_main_notifies_on_nonzero_exit_when_enabled(monkeypatch):
    from app import cli, ops
    calls = []
    monkeypatch.setattr(ops, "notify_failure", lambda code, **k: calls.append(code))
    monkeypatch.setattr(cli, "app", _raiser(1))
    monkeypatch.setenv("CAIRN_NOTIFY", "1")
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 1  # real exit code preserved
    assert calls == [1]


def test_main_does_not_notify_when_disabled(monkeypatch):
    from app import cli, ops
    calls = []
    monkeypatch.setattr(ops, "notify_failure", lambda code, **k: calls.append(code))
    monkeypatch.setattr(cli, "app", _raiser(1))
    monkeypatch.delenv("CAIRN_NOTIFY", raising=False)
    with pytest.raises(SystemExit):
        cli.main()
    assert calls == []


def test_main_does_not_notify_on_success(monkeypatch):
    from app import cli, ops
    calls = []
    monkeypatch.setattr(ops, "notify_failure", lambda code, **k: calls.append(code))
    monkeypatch.setattr(cli, "app", _raiser(0))
    monkeypatch.setenv("CAIRN_NOTIFY", "1")
    with pytest.raises(SystemExit):
        cli.main()
    assert calls == []
