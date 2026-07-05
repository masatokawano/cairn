"""Unattended-run failure notification (M6, DESIGN.md §7 M6 / S4).

The launchd agents (`com.masato.cairn.{sync,weekly}`) run without a human
watching. If a run fails, S4 (「放置しても壊れない」+ 失敗が通知される) requires
that the failure surfaces — otherwise the archive silently stops updating.

Design (§8 「過剰実装しない」): no new daemon, no log tailer. `cli.main()`
already learns the process exit code from Typer; when the run was launched by
an agent (env ``CAIRN_NOTIFY`` truthy) and the code is non-zero, we append a
line to a failures log and post one macOS notification. That is the whole
mechanism.

Keyed on the **exit code, not stderr** on purpose: the agents' stderr is
legitimately noisy (sentence-transformers weight-loading bars, HF Hub
warnings) and append-only stale lines linger there, so "non-empty stderr"
would false-positive constantly. A non-zero exit is the unambiguous signal
(`sync all` already exits 1 iff a source failed; `review weekly` exits 1 only
on a real error, not on a skipped/existing week or a degraded AI draft).
"""
from __future__ import annotations

import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

FAILURES_LOG = "failures.log"
_DEFAULT_LOG_DIR = "~/Library/Logs/cairn"


def should_notify() -> bool:
    """True when this process was launched by an agent that wants alerts."""
    return os.environ.get("CAIRN_NOTIFY", "").strip().lower() in ("1", "true", "yes", "on")


def _log_dir() -> Path:
    return Path(os.environ.get("CAIRN_LOG_DIR", _DEFAULT_LOG_DIR)).expanduser()


def _osascript_notify(title: str, message: str) -> None:
    """Post one macOS notification. Best-effort: osascript absence or failure
    must never turn into a second failure (the run already failed)."""
    subprocess.run(
        ["osascript", "-e",
         f'display notification {_as(message)} with title {_as(title)}'],
        check=False, capture_output=True, timeout=10,
    )


def _as(s: str) -> str:
    """AppleScript string literal: quote and escape backslashes/quotes so
    untrusted text (e.g. an error message) cannot break out of the literal."""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def notify_failure(
    code: int,
    *,
    agent: str | None = None,
    detail: str | None = None,
    notifier=None,
) -> dict:
    """Record and announce a failed agent run. Returns the log record.

    ``notifier(title, message)`` is injectable for tests; production uses the
    osascript poster. The failures log line points back to the agent's stderr
    log, where the traceback/message already lives."""
    agent = agent or os.environ.get("CAIRN_AGENT", "cairn")
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    log_dir = _log_dir()
    stderr_log = log_dir / f"{agent}-error.log"
    line = f"{ts}\t{agent}\texit={code}"
    if detail:
        line += f"\t{' '.join(detail.split())[:200]}"

    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        with (log_dir / FAILURES_LOG).open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass  # a log-write failure must not mask the run's real exit code

    title = f"Cairn: {agent} 同期失敗"
    message = f"exit {code}（{ts}）。詳細: {stderr_log}"
    try:
        (notifier or _osascript_notify)(title, message)
    except Exception:
        pass  # notification is best-effort; never raise from here

    return {"ts": ts, "agent": agent, "code": code, "line": line}
