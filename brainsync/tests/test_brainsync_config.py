from __future__ import annotations

import pytest

from brainsync.config import (
    ConfigError,
    ConfigWarning,
    default_config_path,
    load_config,
    require,
)


def write_config(tmp_path, text: str):
    path = tmp_path / "config.env"
    path.write_text(text, encoding="utf-8")
    return path


def test_parses_bare_and_quoted_values(tmp_path):
    path = write_config(
        tmp_path,
        "\n".join(
            [
                "# コメント行",
                "",
                "KARAKEEP_URL=https://keep.example.com",
                'OBSIDIAN_VAULT="/Users/someone/Obsidian Vault"',
                "ZOTERO_USER_ID='12345'",
            ]
        ),
    )
    config = load_config(path)
    assert config == {
        "KARAKEEP_URL": "https://keep.example.com",
        "OBSIDIAN_VAULT": "/Users/someone/Obsidian Vault",
        "ZOTERO_USER_ID": "12345",
    }


def test_unknown_line_warns_and_is_skipped(tmp_path):
    path = write_config(tmp_path, "KEY=value\nexport OTHER=abc\n")
    with pytest.warns(ConfigWarning):
        config = load_config(path)
    assert config == {"KEY": "value"}


def test_shell_syntax_is_rejected(tmp_path):
    for value in ("$(whoami)", "`whoami`", "$HOME/vault"):
        path = write_config(tmp_path, f"KEY={value}\n")
        with pytest.raises(ConfigError):
            load_config(path)


def test_missing_file_raises(tmp_path):
    with pytest.raises(ConfigError):
        load_config(tmp_path / "nonexistent.env")


def test_environment_is_not_mixed_in(tmp_path, monkeypatch):
    monkeypatch.setenv("INJECTED_FROM_ENV", "should-not-appear")
    path = write_config(tmp_path, "KEY=value\n")
    config = load_config(path)
    assert "INJECTED_FROM_ENV" not in config


def test_default_config_path_env_override(tmp_path, monkeypatch):
    override = tmp_path / "alt.env"
    monkeypatch.setenv("BRAIN_SYNC_CONFIG", str(override))
    assert default_config_path() == override


def test_require_returns_values_in_order():
    config = {"A": "1", "B": "2"}
    assert require(config, "B", "A") == ["2", "1"]


def test_require_raises_on_missing_or_empty():
    with pytest.raises(ConfigError, match="B、Cを確認してください"):
        require({"A": "1", "B": ""}, "A", "B", "C")
