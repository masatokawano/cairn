"""Tests for the backup command (P1-E): a consistent copy that opens and
searches standalone as a separate DB."""
import glob
import importlib
import os

import pytest


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("CAIRN_DB", str(tmp_path / "test.db"))
    from app import db as db_module
    importlib.reload(db_module)
    yield db_module
    conn = getattr(db_module._local, "conn", None)
    if conn:
        conn.close()
        db_module._local.conn = None


def make_conv(db, source_id="c1"):
    from app.parsers.base import ParsedConversation, ParsedMessage
    return ParsedConversation(
        source="chatgpt", source_id=source_id, title="バックアップ対象",
        messages=[ParsedMessage(role="user", text="検索できる本文", created_at="2025-01-01T00:00:00Z")],
        created_at="2025-01-01T00:00:00Z", updated_at="2025-01-01T00:10:00Z",
    )


def _reset_conn(db):
    conn = getattr(db._local, "conn", None)
    if conn:
        conn.close()
        db._local.conn = None


def test_backup_default_path(db, tmp_path):
    db.upsert_conversations([make_conv(db)])
    path = db.backup()
    assert os.path.exists(path)
    assert glob.glob(str(tmp_path / "test.db.backup-*"))
    assert oct(os.stat(path).st_mode & 0o777) == "0o600"


def test_backup_opens_and_searches_standalone(db, tmp_path, monkeypatch):
    db.upsert_conversations([make_conv(db)])
    out = db.backup(str(tmp_path / "snapshot.db"))

    # Mutate the original so we can prove the backup is an independent copy.
    conn = db.connect()
    with conn:
        conn.execute("DELETE FROM messages")
        conn.execute("DELETE FROM conversations")
    _reset_conn(db)

    # Reopen the backup as the active DB and confirm it is fully usable.
    monkeypatch.setenv("CAIRN_DB", out)
    importlib.reload(db)
    results = db.search("本文")
    assert len(results) == 1
    assert results[0]["title"] == "バックアップ対象"
    full = db.get_conversation(results[0]["conversation_id"])
    assert full and full["messages"][0]["text"] == "検索できる本文"
    report = db.integrity_check()
    assert report["ok"] is True


def test_admin_backup_command(db, tmp_path, capsys):
    from app import admin
    importlib.reload(admin)
    db.upsert_conversations([make_conv(db)])
    out = str(tmp_path / "cli-snapshot.db")
    rc = admin.main(["backup", "--out", out])
    assert rc == 0
    assert os.path.exists(out)
    assert "backup:" in capsys.readouterr().out


# --- A1: --with-blobs ----------------------------------------------------------


def test_backup_with_blobs_copies_attachment_tree(db, tmp_path):
    from app import attachments
    h = attachments.store(b"synthetic blob bytes")
    db.upsert_conversations([make_conv(db)])

    out = db.backup(str(tmp_path / "snap.db"), with_blobs=True)
    dest = out + ".attachments"
    copied = os.path.join(dest, h[:2], h)
    assert os.path.isfile(copied)
    with open(copied, "rb") as f:
        assert f.read() == b"synthetic blob bytes"
    assert oct(os.stat(dest).st_mode & 0o777) == "0o700"


def test_backup_with_blobs_missing_store_is_noop(db, tmp_path):
    db.upsert_conversations([make_conv(db)])
    out = db.backup(str(tmp_path / "snap.db"), with_blobs=True)
    assert os.path.exists(out)
    assert not os.path.exists(out + ".attachments")


def test_admin_backup_with_blobs_flag(db, tmp_path, capsys):
    from app import admin, attachments
    importlib.reload(admin)
    attachments.store(b"cli blob")
    db.upsert_conversations([make_conv(db)])
    out = str(tmp_path / "cli-snap.db")
    rc = admin.main(["backup", "--out", out, "--with-blobs"])
    assert rc == 0
    assert os.path.isdir(out + ".attachments")
    stdout = capsys.readouterr().out
    assert "attachments:" in stdout and "(1 blobs)" in stdout


def test_backup_default_names_unique_within_second(db):
    db.upsert_conversations([make_conv(db)])
    first = db.backup()
    second = db.backup()
    assert first != second
    assert os.path.exists(first) and os.path.exists(second)


# --- A8: --keep rotation --------------------------------------------------------


def test_prune_keeps_newest_and_removes_blob_siblings(db, tmp_path):
    from app import attachments
    attachments.store(b"blob for rotation")
    db.upsert_conversations([make_conv(db)])
    backups = [db.backup(with_blobs=True) for _ in range(4)]

    deleted = db.prune_backups(keep=2)
    assert deleted == backups[:2]
    for p in backups[:2]:
        assert not os.path.exists(p)
        assert not os.path.exists(p + ".attachments")
    for p in backups[2:]:
        assert os.path.exists(p)
        assert os.path.isdir(p + ".attachments")


def test_prune_never_touches_explicit_out_backups(db, tmp_path):
    db.upsert_conversations([make_conv(db)])
    manual = db.backup(str(tmp_path / "manual-snapshot.db"))
    auto = [db.backup() for _ in range(3)]

    deleted = db.prune_backups(keep=1)
    assert deleted == auto[:2]
    assert os.path.exists(manual)
    assert os.path.exists(auto[2])


def test_prune_noop_when_under_keep(db):
    db.upsert_conversations([make_conv(db)])
    auto = [db.backup() for _ in range(2)]
    assert db.prune_backups(keep=5) == []
    assert all(os.path.exists(p) for p in auto)


def test_prune_rejects_keep_below_one(db):
    with pytest.raises(ValueError):
        db.prune_backups(keep=0)


def test_admin_backup_keep_flag(db, capsys):
    from app import admin
    importlib.reload(admin)
    db.upsert_conversations([make_conv(db)])
    old = [db.backup() for _ in range(3)]

    rc = admin.main(["backup", "--keep", "2"])
    assert rc == 0
    stdout = capsys.readouterr().out
    # 3 old + 1 new = 4; keep 2 → the 2 oldest are pruned
    assert stdout.count("pruned: ") == 2
    assert not os.path.exists(old[0]) and not os.path.exists(old[1])
    assert os.path.exists(old[2])


def test_admin_backup_keep_zero_rejected(db, capsys):
    from app import admin
    importlib.reload(admin)
    db.upsert_conversations([make_conv(db)])
    before = len([p for p in os.listdir(os.path.dirname(db.DB_PATH))])
    rc = admin.main(["backup", "--keep", "0"])
    assert rc == 2
    # no backup was made and nothing was deleted
    assert len([p for p in os.listdir(os.path.dirname(db.DB_PATH))]) == before
