from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from brainsync.connectors import obsidian


def test_collect_notes_orders_and_links(tmp_path):
    vault = tmp_path
    folder = vault / "External Brain" / "10 Themes"
    folder.mkdir(parents=True)

    newer = folder / "外部脳.md"
    newer.write_text("x", encoding="utf-8")
    older = folder / "sub" / "検索.md"
    older.parent.mkdir()
    older.write_text("x", encoding="utf-8")
    hidden = folder / ".hidden.md"
    hidden.write_text("x", encoding="utf-8")
    stale = folder / "古い.md"
    stale.write_text("x", encoding="utf-8")

    now = datetime.now(tz=timezone.utc)
    old_epoch = (now - timedelta(days=90)).timestamp()
    slightly_old = (now - timedelta(days=1)).timestamp()
    os.utime(stale, (old_epoch, old_epoch))
    os.utime(older, (slightly_old, slightly_old))

    cutoff = now - timedelta(days=30)
    notes = obsidian.collect_notes(vault, folder, cutoff)

    assert [note["title"] for note in notes] == ["外部脳", "検索"]
    assert notes[0]["link"] == "[[External Brain/10 Themes/外部脳]]"
    assert notes[1]["link"] == "[[External Brain/10 Themes/sub/検索]]"


def test_collect_notes_missing_folder(tmp_path):
    cutoff = datetime.now(tz=timezone.utc)
    assert obsidian.collect_notes(tmp_path, tmp_path / "nope", cutoff) == []


def test_write_auto_file_creates_dirs_atomically(tmp_path):
    target = obsidian.write_auto_file(tmp_path, "External Brain", "x.md", "body\n")
    assert target == tmp_path / "External Brain" / "90 Auto" / "x.md"
    assert target.read_text(encoding="utf-8") == "body\n"
    # tmp ファイルが残っていない
    assert list(target.parent.glob("*.tmp")) == []


def test_write_weekly_file_never_overwrites(tmp_path):
    first = obsidian.write_weekly_file(tmp_path, "External Brain", "2099-W01", "v1")
    assert first is not None
    second = obsidian.write_weekly_file(tmp_path, "External Brain", "2099-W01", "v2")
    assert second is None
    assert first.read_text(encoding="utf-8") == "v1"
