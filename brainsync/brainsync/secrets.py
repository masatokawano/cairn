"""macOS Keychain ラッパー。`security find-generic-password` の唯一の呼び出し箇所。

シークレットは config.env・コード・ログに書かない（SECURITY.md）。
"""

from __future__ import annotations

import getpass
import subprocess

KARAKEEP_SERVICE = "brain-sync-karakeep"
ZOTERO_SERVICE = "brain-sync-zotero"


class SecretError(Exception):
    """Keychain からシークレットを取得できなかった。"""


def get_secret(service: str, account: str | None = None) -> str:
    if account is None:
        account = getpass.getuser()
    try:
        result = subprocess.run(
            [
                "security",
                "find-generic-password",
                "-a",
                account,
                "-s",
                service,
                "-w",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except (subprocess.CalledProcessError, OSError) as exc:
        raise SecretError(
            f"Keychain からシークレットを取得できませんでした: service={service}"
        ) from exc
    return result.stdout.strip()
