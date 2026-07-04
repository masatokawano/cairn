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
    (["review", "weekly"], "M4"),
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


def test_sync_obsidian_wired(cli, monkeypatch):
    import json
    from app.connectors import obsidian
    monkeypatch.setattr(obsidian, "sync", lambda: {"source": "obsidian", "inserted": 3})
    runner = CliRunner()
    result = runner.invoke(cli.app, ["sync", "obsidian"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["inserted"] == 3


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


@pytest.fixture()
def sync_all_env(cli, tmp_path, monkeypatch):
    """Vault fixture + fake external connectors for `sync all` tests."""
    vault = tmp_path / "vault"
    (vault / "External Brain" / "90 Auto").mkdir(parents=True)
    (vault / "External Brain" / "10 Themes").mkdir(parents=True)
    monkeypatch.setenv("CAIRN_OBSIDIAN_VAULT", str(vault))
    from app.connectors import karakeep, zotero
    monkeypatch.setattr(karakeep, "sync", lambda **kw: {"source": "karakeep", "skipped": 1})
    monkeypatch.setattr(zotero, "sync", lambda **kw: {"source": "zotero", "skipped": 1})
    return vault


def test_sync_all_runs_every_source_and_writes_lists(cli, sync_all_env):
    import json
    runner = CliRunner()
    result = runner.invoke(cli.app, ["sync", "all"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert set(payload["sources"]) == {"conversations", "karakeep", "zotero", "obsidian"}
    assert payload["failed"] == []
    auto_dir = sync_all_env / "External Brain" / "90 Auto"
    written = {p.name for p in auto_dir.glob("*.md")}
    assert written == {"karakeep-to-review.md", "cairn-recent.md",
                       "zotero-recent.md", "obsidian-context.md"}


def test_sync_all_continues_past_a_failing_source(cli, sync_all_env, monkeypatch):
    import json
    from app.connectors import karakeep
    def boom(**kw):
        raise RuntimeError("connection refused")
    monkeypatch.setattr(karakeep, "sync", boom)
    runner = CliRunner()
    result = runner.invoke(cli.app, ["sync", "all"])
    assert result.exit_code == 1  # failure surfaced ...
    payload = json.loads(result.stdout)
    assert payload["failed"] == ["karakeep"]
    assert "connection refused" in payload["sources"]["karakeep"]
    # ... but every other source still ran, and the lists were still written
    assert payload["sources"]["zotero"] == {"source": "zotero", "skipped": 1}
    assert isinstance(payload["sources"]["obsidian"], dict)
    auto_dir = sync_all_env / "External Brain" / "90 Auto"
    assert len(list(auto_dir.glob("*.md"))) == 4


def test_index_rebuild_runs_on_empty_db(cli):
    """`cairn index rebuild` (M2) succeeds on an empty DB: zero chunks, both
    FTS rebuilds, embeddings gracefully skipped (no provider), links zero."""
    import json
    runner = CliRunner()
    result = runner.invoke(cli.app, ["index", "rebuild"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["chunks_messages"]["chunks"] == 0
    assert payload["chunks_items"]["chunks"] == 0
    assert isinstance(payload["embeddings"], str) and "skipped" in payload["embeddings"]
    assert payload["item_links"]["total"] == 0
