"""config.env の非実行パーサ。

旧実装は `bash -c 'source config.env; env -0'` で読んでいたが、config.env の
内容がシェルとして評価される経路だったため廃止した（INTEGRATION.md T2-2）。
`KEY=value` / `KEY="value"` / `KEY='value'` 形式のみを受け付け、
シェル構文（`$` 展開・バッククォート）は不許可。環境変数は混ぜず、
ファイル内で定義されたキーだけを返す。
"""

from __future__ import annotations

import os
import re
import warnings
from pathlib import Path

_LINE_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


class ConfigError(Exception):
    """config.env が読めない・書式が不正。"""


class ConfigWarning(UserWarning):
    """解釈できない行の警告（該当行は無視される）。"""


def default_config_path() -> Path:
    """config.env の既定位置（パッケージの親 = brainsync/ サブツリー直下）。

    環境変数 BRAIN_SYNC_CONFIG で上書きできる（テスト・別配置用）。
    """
    override = os.environ.get("BRAIN_SYNC_CONFIG")
    if override:
        return Path(override)
    return Path(__file__).resolve().parent.parent / "config.env"


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def load_config(path: Path | None = None) -> dict[str, str]:
    """config.env を読み、ファイル内のキーのみを返す。

    - 空行・`#` 始まりの行は無視
    - `KEY=value` に一致しない行は ConfigWarning を出して無視
    - 値に `$` またはバッククォートを含む行は ConfigError
      （旧 bash 実装ではシェル展開されていた書式。展開はしないので拒否する）
    """
    if path is None:
        path = default_config_path()
    if not path.is_file():
        raise ConfigError(f"config.env がありません: {path}")

    config: dict[str, str] = {}
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue

        match = _LINE_RE.match(line)
        if match is None:
            warnings.warn(
                f"{path.name}:{lineno}: 解釈できない行を無視しました: {line!r}",
                ConfigWarning,
                stacklevel=2,
            )
            continue

        key, value = match.group(1), _unquote(match.group(2).strip())
        if "$" in value or "`" in value:
            raise ConfigError(
                f"{path.name}:{lineno}: シェル構文は使えません"
                f"（$ やバッククォートを含む値は不許可）: {key}"
            )
        config[key] = value

    return config


def require(config: dict[str, str], *keys: str) -> list[str]:
    """必須キーの値を返す。欠けていれば ConfigError。"""
    missing = [key for key in keys if not config.get(key)]
    if missing:
        raise ConfigError("、".join(missing) + "を確認してください")
    return [config[key] for key in keys]
