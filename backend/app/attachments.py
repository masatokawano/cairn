"""Filesystem blob store for attachment bytes (P1-J: attached blob store).

Cairn stores attachment metadata (`attachments` table) and the conversation
text that mentions an attachment, but until now had no way to keep the
underlying bytes — so PDFs, images, and uploaded files were lost on import.
This module is the byte-side counterpart to the metadata table.

Layout: `data/attachments/{hash[:2]}/{hash}` — a git-style 2-char fan-out
so a single directory's child count stays bounded at 256 even with 100k+
blobs. The hash IS the filename; storing the same content twice writes
the same path, dedup-by-design. Tracking pointers in a column would
duplicate this property and risk drift.

Permissions follow the DB convention (0600): the blob store sits next to
cairn.db and a stolen $HOME backup must not expose conversation contents.

This module is intentionally I/O-only — it knows nothing about
attachments rows. db.upsert_conversations calls store() during ingest;
integrity_check() reads back to compare disk against table.
"""
from __future__ import annotations

import hashlib
import logging
import os
from collections.abc import Iterator

log = logging.getLogger("cairn.attachments")

# Read CAIRN_DB lazily so tests that monkeypatch the env between modules
# see the up-to-date value. Mirrors the resolution in db.py — we re-derive
# instead of importing to avoid the circular `db ↔ attachments` edge.
_DEFAULT_DB = os.path.join(os.path.dirname(__file__), "..", "data", "cairn.db")


def root_dir() -> str:
    """Path of the attachments directory. Sibling of cairn.db so admin
    backup can colocate them and the user can move them as a pair."""
    db_path = os.environ.get("CAIRN_DB", _DEFAULT_DB)
    return os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(db_path)), "attachments"))


def path_for(sha256_hex: str) -> str:
    """Filesystem path for a blob with this sha256. Does not check existence."""
    return os.path.join(root_dir(), sha256_hex[:2], sha256_hex)


def has(sha256_hex: str) -> bool:
    return os.path.isfile(path_for(sha256_hex))


def store(blob: bytes) -> str:
    """Persist `blob` to the store. Returns its sha256 hex.

    Idempotent: if a blob with the same hash already exists, this is a
    no-op. Writes go through a `.tmp` sibling + atomic rename so a crash
    mid-write doesn't leave a half-blob that would later masquerade as a
    valid one.
    """
    h = hashlib.sha256(blob).hexdigest()
    target = path_for(h)
    if os.path.isfile(target):
        return h
    os.makedirs(os.path.dirname(target), exist_ok=True)
    tmp = target + ".tmp"
    with open(tmp, "wb") as f:
        f.write(blob)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, target)
    return h


def get(sha256_hex: str) -> bytes | None:
    """Read a blob back. Returns None if not present (no exception)."""
    target = path_for(sha256_hex)
    if not os.path.isfile(target):
        return None
    with open(target, "rb") as f:
        return f.read()


def iter_hashes() -> Iterator[str]:
    """Yield every blob's sha256 currently on disk. Used by integrity check
    to detect orphans (file exists but no row references it)."""
    root = root_dir()
    if not os.path.isdir(root):
        return
    for shard in os.listdir(root):
        shard_dir = os.path.join(root, shard)
        if not os.path.isdir(shard_dir):
            continue
        for name in os.listdir(shard_dir):
            if name.endswith(".tmp"):
                continue  # in-flight write; skip until atomically renamed
            yield name
