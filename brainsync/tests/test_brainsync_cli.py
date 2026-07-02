from __future__ import annotations

import pytest

from brainsync import cli


@pytest.fixture
def tmp_env(tmp_path, monkeypatch):
    """tmp の vault + config.env を BRAIN_SYNC_CONFIG 経由で使わせる。"""
    vault = tmp_path / "vault"
    (vault / "External Brain" / "90 Auto").mkdir(parents=True)

    config = tmp_path / "config.env"
    config.write_text(
        "\n".join(
            [
                f'OBSIDIAN_VAULT="{vault}"',
                'OBSIDIAN_EXTERNAL_BRAIN_DIR="External Brain"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("BRAIN_SYNC_CONFIG", str(config))
    return vault


def test_sync_obsidian_end_to_end(tmp_env, capsys):
    themes = tmp_env / "External Brain" / "10 Themes"
    themes.mkdir(parents=True)
    (themes / "テーマA.md").write_text("x", encoding="utf-8")

    assert cli.main(["sync-obsidian"]) == 0

    target = tmp_env / "External Brain" / "90 Auto" / "obsidian-context.md"
    content = target.read_text(encoding="utf-8")
    assert "theme_count: 1" in content
    assert "[[External Brain/10 Themes/テーマA]]" in content

    out = capsys.readouterr().out
    assert f"Created: {target}" in out
    assert "Themes: 1" in out


def test_weekly_creates_then_protects(tmp_env, monkeypatch, capsys):
    monkeypatch.setenv("BRAIN_SYNC_WEEK", "2099-W10")

    assert cli.main(["weekly"]) == 0
    target = tmp_env / "External Brain" / "40 Reviews/Weekly" / "2099-W10.md"
    assert target.exists()
    assert "Created:" in capsys.readouterr().out

    assert cli.main(["weekly"]) == 0
    assert "Weekly review already exists:" in capsys.readouterr().out


def test_missing_config_returns_1(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("BRAIN_SYNC_CONFIG", str(tmp_path / "nope.env"))
    assert cli.main(["sync-obsidian"]) == 1
    assert "config.env がありません" in capsys.readouterr().err


def test_missing_required_key_returns_1(tmp_path, monkeypatch, capsys):
    config = tmp_path / "config.env"
    config.write_text('OBSIDIAN_VAULT="/tmp/v"\n', encoding="utf-8")
    monkeypatch.setenv("BRAIN_SYNC_CONFIG", str(config))

    assert cli.main(["sync-obsidian"]) == 1
    assert "OBSIDIAN_EXTERNAL_BRAIN_DIR" in capsys.readouterr().err


def test_check_rejects_unknown_target(tmp_env):
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["check", "unknown"])
    assert excinfo.value.code == 2


def test_check_obsidian_writes_test_file(tmp_env, capsys):
    assert cli.main(["check", "obsidian"]) == 0
    target = tmp_env / "External Brain" / "90 Auto" / "brain-sync-test.md"
    content = target.read_text(encoding="utf-8")
    assert "type: connection-test" in content
    assert "# Brain Sync 接続テスト" in content


def test_check_obsidian_requires_existing_dir(tmp_path, monkeypatch, capsys):
    config = tmp_path / "config.env"
    config.write_text(
        f'OBSIDIAN_VAULT="{tmp_path / "empty-vault"}"\n'
        'OBSIDIAN_EXTERNAL_BRAIN_DIR="External Brain"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("BRAIN_SYNC_CONFIG", str(config))

    assert cli.main(["check", "obsidian"]) == 1
    assert "対象ディレクトリが見つかりません" in capsys.readouterr().err
