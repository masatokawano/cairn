"""connectors/obsidian.py — fixture vault, no real vault access (M3).

Covers: indexed vs excluded directories, mtime+hash diff, rename/delete
pruning, redaction via the upsert_items choke point, read-only-ness (the
connector must never create/modify files in the vault), search integration.
"""
import importlib
import json
import os

import pytest


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("CAIRN_DB", str(tmp_path / "test.db"))
    vault = tmp_path / "vault"
    (vault / "External Brain" / "10 Themes").mkdir(parents=True)
    (vault / "External Brain" / "20 Projects").mkdir(parents=True)
    (vault / "External Brain" / "00 Inbox" / "Ideas").mkdir(parents=True)
    (vault / "External Brain" / "50 Decisions").mkdir(parents=True)
    (vault / "External Brain" / "90 Auto").mkdir(parents=True)
    (vault / "External Brain" / "40 Reviews" / "Weekly").mkdir(parents=True)
    monkeypatch.setenv("CAIRN_OBSIDIAN_VAULT", str(vault))
    from app import db as db_module
    importlib.reload(db_module)
    yield db_module
    conn = getattr(db_module._local, "conn", None)
    if conn:
        conn.close()
        db_module._local.conn = None


def vault_root():
    from pathlib import Path
    return Path(os.environ["CAIRN_OBSIDIAN_VAULT"])


def write_note(rel: str, text: str):
    path = vault_root() / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def sync():
    from app.connectors import obsidian
    return obsidian.sync()


def note_items(db):
    return {
        r["external_id"]: r
        for r in db.connect().execute(
            "SELECT * FROM items WHERE source='obsidian'").fetchall()
    }


def test_indexes_target_dirs_and_skips_auto_reviews(db):
    write_note("External Brain/10 Themes/外部脳.md", "外部脳の設計テーマ")
    write_note("External Brain/20 Projects/cairn.md", "Cairn 統合プロジェクト")
    write_note("External Brain/00 Inbox/Ideas/着想.md", "朝の着想メモ")
    write_note("External Brain/50 Decisions/D9.md", "FDA を廃止する決定")
    # excluded: generated output must never flow back into the index
    write_note("External Brain/90 Auto/cairn-recent.md", "自動生成一覧")
    write_note("External Brain/90 Auto/Health/current-status.md",
               "未承認の自動健康レポート")
    # Human-approved summaries enter Cairn only through a normal note path.
    write_note("External Brain/10 Themes/approved-health-summary.md",
               "人間が確認した健康サマリー")
    write_note("External Brain/40 Reviews/Weekly/2026-W27.md", "週次レビュー")
    write_note("External Brain/30 Sources/引用元.md", "対象外ディレクトリ")

    stats = sync()
    assert stats["inserted"] == 5
    items = note_items(db)
    assert set(items) == {
        "External Brain/10 Themes/外部脳.md",
        "External Brain/10 Themes/approved-health-summary.md",
        "External Brain/20 Projects/cairn.md",
        "External Brain/00 Inbox/Ideas/着想.md",
        "External Brain/50 Decisions/D9.md",
    }
    theme = items["External Brain/10 Themes/外部脳.md"]
    assert theme["kind"] == "note"
    assert theme["title"] == "外部脳"
    meta = json.loads(theme["meta"])
    assert meta["folder"] == "10 Themes"  # External Brain 相対の索引フォルダ
    assert "設計テーマ" in meta["text"]


def test_resync_unchanged_all_skips_and_no_vault_writes(db):
    write_note("External Brain/10 Themes/a.md", "本文A")
    sync()
    before = sorted(p.as_posix() for p in vault_root().rglob("*") if p.is_file())
    stats = sync()
    assert stats["inserted"] == 0 and stats["updated"] == 0
    assert stats["skipped"] >= 1
    assert stats["links"] is None
    # read-only invariant: the vault byte-for-byte file list is untouched
    after = sorted(p.as_posix() for p in vault_root().rglob("*") if p.is_file())
    assert before == after


def test_edit_detected_via_content_hash(db):
    path = write_note("External Brain/10 Themes/a.md", "旧本文")
    sync()
    path.write_text("新本文", encoding="utf-8")
    stats = sync()
    assert stats["updated"] == 1
    meta = json.loads(note_items(db)["External Brain/10 Themes/a.md"]["meta"])
    assert meta["text"] == "新本文"


def test_rename_prunes_ghost_item(db):
    path = write_note("External Brain/10 Themes/旧名.md", "本文")
    sync()
    path.rename(vault_root() / "External Brain/10 Themes/新名.md")
    stats = sync()
    assert stats["pruned"] == 1
    items = note_items(db)
    assert set(items) == {"External Brain/10 Themes/新名.md"}
    # pruned item's chunks are gone too
    orphan = db.connect().execute(
        "SELECT COUNT(*) FROM chunks ch LEFT JOIN items i ON i.id = ch.item_id"
        " WHERE ch.kind='item_text' AND i.id IS NULL").fetchone()[0]
    assert orphan == 0


def test_delete_prunes_item(db):
    path = write_note("External Brain/10 Themes/消える.md", "本文")
    sync()
    path.unlink()
    stats = sync()
    assert stats["pruned"] == 1
    assert note_items(db) == {}


def test_note_text_is_redacted(db):
    write_note("External Brain/10 Themes/秘密.md",
               "キーは sk-ant-api03-abcdefghijklmnopqrstuvwx です")
    sync()
    meta = note_items(db)["External Brain/10 Themes/秘密.md"]["meta"]
    assert "sk-ant-" not in meta
    assert "[REDACTED:anthropic]" in meta


def test_notes_are_searchable_cross_source(db):
    write_note("External Brain/10 Themes/検索テーマ.md", "notesearchable な独自語彙")
    sync()
    res = db.search("notesearchable")
    assert [r["kind"] for r in res] == ["note"]
    assert res[0]["source"] == "obsidian"
    assert res[0]["url"] is None


def test_missing_vault_env_raises(db, monkeypatch):
    from app.connectors import ConnectorError
    monkeypatch.delenv("CAIRN_OBSIDIAN_VAULT")
    with pytest.raises(ConnectorError):
        sync()
    assert "CAIRN_OBSIDIAN_VAULT" in db.get_sync_state("obsidian")["last_error"]


def test_dotfiles_skipped(db):
    write_note("External Brain/10 Themes/.trash.md", "ゴミ")
    write_note("External Brain/10 Themes/.hidden/実体.md", "隠し")
    write_note("External Brain/10 Themes/正規.md", "本文")
    stats = sync()
    assert stats["inserted"] == 1
    assert set(note_items(db)) == {"External Brain/10 Themes/正規.md"}
