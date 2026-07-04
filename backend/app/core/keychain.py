"""macOS Keychain lookup for connector API keys (M1, DESIGN.md §5.1 / D8).

Thin subprocess wrapper over ``security find-generic-password``. Services in
use: ``brain-sync-karakeep`` and ``brain-sync-zotero`` (D8: the legacy names
are kept on purpose). The secret value must never reach logs, config files,
or exception messages — errors carry only the service name and exit code,
and the subprocess stderr is discarded rather than re-raised.
"""
from __future__ import annotations

import getpass
import subprocess


class KeychainError(RuntimeError):
    """Lookup failed. The message never contains secret material."""


def get_secret(service: str, account: str | None = None) -> str:
    account = account or getpass.getuser()
    try:
        proc = subprocess.run(
            ["security", "find-generic-password", "-a", account, "-s", service, "-w"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise KeychainError(
            f"keychain lookup failed for service {service!r}: {type(exc).__name__}"
        ) from None
    if proc.returncode != 0:
        raise KeychainError(
            f"keychain lookup failed for service {service!r} (exit {proc.returncode}); "
            f"is the item present? try: security find-generic-password -s {service}"
        )
    secret = proc.stdout.strip()
    if not secret:
        raise KeychainError(f"keychain returned an empty secret for service {service!r}")
    return secret
