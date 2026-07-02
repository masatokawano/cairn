"""JSON API への HTTP GET（標準ライブラリのみ）。

connector はこのモジュールの get_json をデフォルト引数として受け取り、
テストでは同シグネチャの偽物に差し替える。
"""

from __future__ import annotations

import json
import urllib.request
from datetime import datetime
from typing import Any


def get_json(
    url: str,
    headers: dict[str, str] | None = None,
    timeout: float = 30,
) -> tuple[Any, dict[str, str]]:
    """URL を GET して (parsed JSON, response headers) を返す。"""
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", **(headers or {})},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response), dict(response.headers)


def ping(url: str, timeout: float = 5) -> bool:
    """URL に到達できるかだけを確認する（本文は捨てる）。"""
    try:
        request = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=timeout):
            return True
    except OSError:
        return False


def parse_timestamp(value: str) -> datetime:
    """API の ISO8601 文字列（Z 終端可）を datetime にする。"""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
