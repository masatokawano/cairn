"""Tests for the `cairn` CLI (backend/app/cli.py).

Wired: `sync conversations` (M0) and `sync karakeep` / `sync zotero` (M1,
tested here with the connector monkeypatched — connector internals are
covered in test_connector_*.py). Remaining subcommands must exit 1 with a
milestone-tagged message. `--help` must exit 0.
"""
import importlib

import pytest
from typer.testing import CliRunner


@pytest.fixture()
def cli(tmp_path, monkeypatch):
    monkeypatch.setenv("CAIRN_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("CAIRN_CLAUDE_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("CAIRN_CODEX_DIR", str(tmp_path / "codex"))
    from app import db as db_module
    from app import cli_sync as cli_sync_module
    from app import cli as cli_module
    importlib.reload(db_module)
    importlib.reload(cli_sync_module)
    importlib.reload(cli_module)
    yield cli_module
    conn = getattr(db_module._local, "conn", None)
    if conn:
        conn.close()
        db_module._local.conn = None


def test_help_exits_zero(cli):
    runner = CliRunner()
    result = runner.invoke(cli.app, ["--help"])
    assert result.exit_code == 0
    assert "sync" in result.stdout
    assert "review" in result.stdout
    assert "index" in result.stdout


@pytest.mark.parametrize("args,milestone", [
    (["sync", "obsidian"], "M3"),
    (["sync", "all"], "M3"),
    (["review", "weekly"], "M4"),
    (["index", "rebuild"], "M2"),
])
def test_stub_subcommands_exit_one(cli, args, milestone):
    runner = CliRunner()
    result = runner.invoke(cli.app, args)
    assert result.exit_code == 1, f"{' '.join(args)} should exit 1, got {result.exit_code}"
    # stub messages go to stderr; CliRunner mixes streams by default.
    combined = result.stdout + (result.stderr if result.stderr_bytes is not None else "")
    assert milestone in combined


@pytest.mark.parametrize("name", ["karakeep", "zotero"])
def test_sync_connector_success_prints_stats(cli, monkeypatch, name):
    import json
    from app.connectors import karakeep, zotero
    module = {"karakeep": karakeep, "zotero": zotero}[name]
    seen = {}

    def fake_sync(*, full=False, **kwargs):
        seen["full"] = full
        return {"source": name, "inserted": 2}

    monkeypatch.setattr(module, "sync", fake_sync)
    runner = CliRunner()
    result = runner.invoke(cli.app, ["sync", name, "--full"])
    assert result.exit_code == 0, result.output
    assert seen["full"] is True
    assert json.loads(result.stdout)["inserted"] == 2


@pytest.mark.parametrize("name", ["karakeep", "zotero"])
def test_sync_connector_failure_exits_nonzero(cli, monkeypatch, name):
    from app.connectors import karakeep, zotero
    module = {"karakeep": karakeep, "zotero": zotero}[name]

    def fake_sync(**kwargs):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(module, "sync", fake_sync)
    runner = CliRunner()
    result = runner.invoke(cli.app, ["sync", name])
    assert result.exit_code == 1
    combined = result.stdout + (result.stderr if result.stderr_bytes is not None else "")
    assert "sync failed" in combined


def test_sync_conversations_runs_end_to_end(cli):
    """Empty log dirs → scan returns zero-count stats, exit 0. Proves the
    CLI is wired to cli_sync.scan_once (Phase-1 code) end-to-end without
    requiring fixture logs."""
    import json
    runner = CliRunner()
    result = runner.invoke(cli.app, ["sync", "conversations"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["files_scanned"] == 0
    assert payload["files_imported"] == 0
    assert payload["inserted"] == 0
