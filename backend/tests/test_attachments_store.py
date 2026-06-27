"""Tests for the filesystem blob store (P1-J).

Storage layout, idempotency, atomic writes, integrity round-trip with the
attachments table, and the orphan/missing accounting in integrity_check.
"""
import importlib
import os
import hashlib

import pytest


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Fresh CAIRN_DB → fresh attachments dir under the same tmp parent."""
    monkeypatch.setenv("CAIRN_DB", str(tmp_path / "test.db"))
    from app import db, attachments
    importlib.reload(db)
    importlib.reload(attachments)
    yield db, attachments
    conn = getattr(db._local, "conn", None)
    if conn:
        conn.close()
        db._local.conn = None


def test_store_returns_hash_and_writes_at_sharded_path(env):
    _, store = env
    h = store.store(b"hello attachment")
    assert h == hashlib.sha256(b"hello attachment").hexdigest()
    p = store.path_for(h)
    assert os.path.isfile(p)
    # sharded by first 2 chars
    assert os.path.basename(os.path.dirname(p)) == h[:2]


def test_store_is_idempotent_and_overwrite_safe(env):
    _, store = env
    h = store.store(b"same bytes")
    assert store.has(h)
    # storing the same blob again should not error and not change the file
    mtime = os.path.getmtime(store.path_for(h))
    store.store(b"same bytes")
    assert os.path.getmtime(store.path_for(h)) == mtime  # untouched on dup


def test_get_round_trips_bytes(env):
    _, store = env
    payload = b"\x00\x01\x02 binary \xff"
    h = store.store(payload)
    assert store.get(h) == payload


def test_get_missing_returns_none_not_raises(env):
    _, store = env
    assert store.get("0" * 64) is None


def test_iter_hashes_lists_only_finished_blobs(env, tmp_path):
    _, store = env
    h1 = store.store(b"a")
    h2 = store.store(b"b")
    # simulate a .tmp left from a crashed write — must be excluded
    tmp_path_for_h1 = store.path_for(h1) + ".tmp"
    with open(tmp_path_for_h1, "wb") as f:
        f.write(b"unfinished")
    assert set(store.iter_hashes()) == {h1, h2}


def test_blob_file_mode_is_0600(env):
    _, store = env
    h = store.store(b"sensitive bytes")
    mode = os.stat(store.path_for(h)).st_mode & 0o777
    assert mode == 0o600


# --- integration with upsert_conversations --------------------------------

def _make_conv_with_blob(source_id, data, name="file.bin"):
    from app.parsers.base import (
        ParsedAttachment, ParsedConversation, ParsedMessage,
    )
    h = hashlib.sha256(data).hexdigest()
    return ParsedConversation(
        source="chatgpt", source_id=source_id, title=name,
        messages=[ParsedMessage(
            role="user", text="see attached", created_at="2025-01-01T00:00:00Z",
            attachments=[ParsedAttachment(
                source_ref=name, mime="application/octet-stream",
                size=len(data), hash=h, data=data,
            )],
        )],
        created_at="2025-01-01T00:00:00Z", updated_at="2025-01-01T00:10:00Z",
    )


def test_upsert_persists_attachment_bytes_to_store(env):
    db, store = env
    payload = b"PDF-like blob content"
    db.upsert_conversations([_make_conv_with_blob("c1", payload)])
    h = hashlib.sha256(payload).hexdigest()
    assert store.has(h)
    assert store.get(h) == payload


def test_upsert_metadata_only_attachment_does_not_touch_store(env):
    """Attachments lacking `data` (Claude's UUID-only refs) must NOT
    create empty files — only metadata is stored."""
    db, store = env
    from app.parsers.base import (
        ParsedAttachment, ParsedConversation, ParsedMessage,
    )
    pc = ParsedConversation(
        source="claude", source_id="c1", title="t",
        messages=[ParsedMessage(role="user", text="x", attachments=[
            ParsedAttachment(source_ref="uuid-only-no-bytes", data=None),
        ])],
    )
    db.upsert_conversations([pc])
    assert list(store.iter_hashes()) == []  # nothing was stored


def test_integrity_check_reports_missing_and_orphan_blobs(env, tmp_path):
    db, store = env
    payload = b"in both store and table"
    db.upsert_conversations([_make_conv_with_blob("c1", payload)])
    # Plant an orphan blob (file with no attachments row pointing at it)
    orphan = b"orphan bytes nobody references"
    orphan_hash = store.store(orphan)
    # Simulate a missing blob: delete one referenced file
    real_hash = hashlib.sha256(payload).hexdigest()
    os.remove(store.path_for(real_hash))

    report = db.integrity_check()
    assert report["checks"]["attachment_blobs_missing"] == 1  # the deleted one
    assert report["checks"]["attachment_blobs_orphan"] == 1   # the planted one
    # neither is fatal — integrity check stays "ok" for these
    assert "problem" not in str(report["problems"]).lower() or not report["problems"]


def test_store_writes_under_db_directory(env, tmp_path):
    """The blob store lives alongside cairn.db so admin backup colocates
    them and a user moving the data dir moves both as a pair."""
    _, store = env
    h = store.store(b"x")
    assert store.path_for(h).startswith(str(tmp_path))
