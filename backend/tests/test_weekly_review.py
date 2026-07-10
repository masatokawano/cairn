"""deliver/weekly_review.py — the M4 weekly review (DESIGN.md §5.4).

Completion conditions pinned here: the §5.4 section structure, related items
older than 14 days with a per-item reason line, the new-only policy for an
existing week, provenance labelling of the AI draft, and — critically — that
the review still generates when the LLM fails or ollama is unreachable (S4).
"""
import importlib
from datetime import datetime, timezone

import pytest

# Monday noon UTC → the most recently closed week (Sunday 18:00 close) is
# 2026-W27 in every timezone the suite may run in.
NOW = datetime(2026, 7, 6, 12, 0, 0, tzinfo=timezone.utc)
OLD = "2026-05-01T00:00:00Z"
THIS_WEEK = "2026-07-02T00:00:00Z"

DRAFT = {
    "recurring_themes": ["外部脳と検索の話"],
    "new_ideas": ["週次の再発見を自動化する"],
    "changed_views": [],
    "open_questions": ["埋め込みモデルの更新方針"],
    "next_week_candidates": ["M5 に着手する"],
}


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("CAIRN_DB", str(tmp_path / "test.db"))
    vault = tmp_path / "vault"
    (vault / "External Brain").mkdir(parents=True)
    monkeypatch.setenv("CAIRN_OBSIDIAN_VAULT", str(vault))
    from app import db as db_module
    importlib.reload(db_module)
    yield db_module, vault
    conn = getattr(db_module._local, "conn", None)
    if conn:
        conn.close()
        db_module._local.conn = None


def weekly_dir(vault):
    return vault / "External Brain" / "40 Reviews" / "Weekly"


def add_item(db, source, kind, external_id, *, title, url=None,
             created=OLD, updated=OLD, **meta):
    stats = db.upsert_items(source, kind, [{
        "external_id": external_id, "title": title, "url": url,
        "created_at": created, "updated_at": updated, "meta": meta,
    }])
    db.rechunk_items(stats["changed_ids"], force=True)


def make_conv(db, source_id, *, title, text="本文", n_messages=5,
              updated=THIS_WEEK):
    from app.parsers.base import ParsedConversation, ParsedMessage
    db.upsert_conversations([ParsedConversation(
        source="chatgpt", source_id=source_id, title=title,
        messages=[ParsedMessage(role="user", text=f"{text} {i}", created_at=updated)
                  for i in range(n_messages)],
        created_at=updated, updated_at=updated, meta={},
    )])


def fixture_llm(responses=None, fail=0):
    from app.llm.fixture import FixtureProvider
    return FixtureProvider(fail_first=fail, responses=list(responses or []))


def seed_week(db):
    """One conversation this week + one old bookmark it should resurface."""
    make_conv(db, "conv-1", title="外部脳の週次レビュー設計",
              text="外部脳の週次レビュー設計について")
    add_item(db, "karakeep", "bookmark", "past-bm", title="外部脳の記事",
             url="https://example.com/brain", text="外部脳の記事")


def test_review_structure_and_related_with_reason(env):
    db, vault = env
    seed_week(db)
    from app.deliver import weekly_review
    out = weekly_review.run(now=NOW, llm=fixture_llm([DRAFT]))
    assert out["status"] == "created"
    assert out["week"] == "2026-W27"
    md = (weekly_dir(vault) / "2026-W27.md").read_text()
    # §5.4 構成
    assert "# 2026-W27 週次レビュー" in md
    assert "## 今週の活動" in md
    assert "### 発見（Karakeep, to-review 優先, ≤10件）" in md
    assert "### 思考（Cairn 会話, ≤10件）" in md
    assert "### 根拠（Zotero, ≤10件）" in md
    assert "### 理解（Obsidian 更新ノート, ≤10件）" in md
    assert "## 過去からの関連" in md
    assert "## 統合メモ（AI草案 — 編集・削除自由）" in md
    # this week's conversation appears as activity
    assert "外部脳の週次レビュー設計" in md
    # the OLD bookmark resurfaces with a reason line
    assert "外部脳の記事" in md
    assert "- なぜ: 今週の「" in md
    assert out["related_count"] >= 1
    # draft rendered with provenance (§6.2)
    assert "<!-- generated_by: cairn/fixture-v1/prompt_v1 -->" in md
    assert "外部脳と検索の話" in md
    assert "### 見解の変化" in md  # empty draft list still gets its heading


def test_existing_week_is_never_overwritten(env):
    db, vault = env
    from app.deliver import weekly_review
    target_dir = weekly_dir(vault)
    target_dir.mkdir(parents=True)
    (target_dir / "2026-W27.md").write_text("手書きの内容")
    out = weekly_review.run(now=NOW, llm=fixture_llm([DRAFT]))
    assert out["status"] == "exists"
    assert (target_dir / "2026-W27.md").read_text() == "手書きの内容"


def test_llm_failure_degrades_but_review_is_written(env):
    db, vault = env
    seed_week(db)
    from app.deliver import weekly_review
    out = weekly_review.run(now=NOW, llm=fixture_llm(fail=99))
    assert out["status"] == "created"
    assert out["draft"].startswith("failed:")
    md = (weekly_dir(vault) / "2026-W27.md").read_text()
    assert "AI 草案の生成に失敗しました" in md
    assert "generated_by" not in md          # nothing was generated
    assert "## 過去からの関連" in md          # rest of the review intact
    assert "外部脳の記事" in md


def test_ollama_unreachable_still_generates(env, monkeypatch):
    """完了条件: ollama 停止状態でもレビュー生成自体は成功する。
    Points the real OllamaProvider at a dead port (nothing listens on :1)."""
    db, vault = env
    monkeypatch.setenv("CAIRN_OLLAMA_HOST", "http://127.0.0.1:1")
    from app.deliver import weekly_review
    out = weekly_review.run(now=NOW)  # no injected llm → real provider path
    assert out["status"] == "created"
    assert out["draft"].startswith("failed:")
    assert (weekly_dir(vault) / "2026-W27.md").exists()


def test_week_option_sets_filename_and_window(env):
    db, vault = env
    make_conv(db, "conv-x", title="遠い未来の議論", updated="2099-01-01T00:00:00Z")
    from app.deliver import weekly_review
    out = weekly_review.run(week="2099-W01", llm=fixture_llm([DRAFT]))
    assert out["status"] == "created"
    md = (weekly_dir(vault) / "2099-W01.md").read_text()
    # 2099-W01 runs Mon 2098-12-29 .. Sun 2099-01-04: the conversation is in
    assert "遠い未来の議論" in md
    # NOW-era week untouched
    assert not (weekly_dir(vault) / "2026-W27.md").exists()


def test_default_week_is_most_recently_closed(env):
    """レビュー指摘 3.1: 週の途中（ログイン時 RunAtLoad など）に実行しても
    進行中の週を早期生成しない — 対象は直近に締まった週（前週）になる。"""
    db, vault = env
    from app.deliver import weekly_review
    saturday = datetime(2026, 7, 4, 12, 0, tzinfo=timezone.utc)  # W27 の土曜
    out = weekly_review.run(now=saturday, llm=fixture_llm([DRAFT]))
    assert out["week"] == "2026-W26"       # 進行中の W27 ではなく前週
    assert not (weekly_dir(vault) / "2026-W27.md").exists()


def test_sunday_close_hour_switches_target_week(env):
    """日曜はローカル18:00（launchd の定時）を境に対象週が切り替わる。"""
    db, vault = env
    from app.deliver import weekly_review
    local_tz = datetime.now().astimezone().tzinfo
    morning = datetime(2026, 7, 5, 9, 0, tzinfo=local_tz)    # 日曜 9:00
    evening = datetime(2026, 7, 5, 18, 30, tzinfo=local_tz)  # 日曜 18:30
    assert weekly_review._resolve_week(None, morning)[0] == "2026-W26"
    week_id, ref = weekly_review._resolve_week(None, evening)
    assert week_id == "2026-W27"
    # ref は対象週の日曜 23:59:59（ローカル）— --week 指定時と同じ基準点
    assert (ref.year, ref.month, ref.day, ref.hour) == (2026, 7, 5, 23)


def test_week_option_rejects_garbage(env):
    from app.deliver import weekly_review
    with pytest.raises(ValueError):
        weekly_review.run(week="2026-27")
    with pytest.raises(ValueError):
        weekly_review.run(week="2026-W99")


def test_untrusted_titles_and_llm_output_are_escaped(env):
    db, vault = env
    evil_title = "注入\n# 偽見出し [x](https://evil/) `rm -rf`"
    make_conv(db, "conv-evil", title=evil_title, text="注入の議論")
    evil_draft = {
        "recurring_themes": ["<!-- 偽コメント --> ![img](https://evil/i)"],
        "new_ideas": ["改行\n# 見出し注入"],
        "changed_views": [], "open_questions": [], "next_week_candidates": [],
    }
    from app.deliver import weekly_review
    out = weekly_review.run(now=NOW, llm=fixture_llm([evil_draft]))
    assert out["status"] == "created"
    md = (weekly_dir(vault) / "2026-W27.md").read_text()
    assert "\n# 偽見出し" not in md
    assert "\n# 見出し注入" not in md
    assert "[x](" not in md
    assert "![img](" not in md
    assert "`rm -rf`" not in md
    assert "<!-- 偽コメント -->" not in md


def test_stale_export_warning(env):
    db, vault = env
    # chatgpt imported recently → no warning; claude 60+ days ago → warning;
    # gemini never → "一度も" warning.
    db.record_import_run(source="chatgpt", input_name="x",
                         started_at="2026-07-01T00:00:00Z",
                         completed_at="2026-07-01T00:00:00Z")
    db.record_import_run(source="claude", input_name="x",
                         started_at="2026-05-01T00:00:00Z",
                         completed_at="2026-05-01T00:00:00Z")
    from app.deliver import weekly_review
    out = weekly_review.run(now=NOW, llm=fixture_llm([DRAFT]))
    md = (weekly_dir(vault) / "2026-W27.md").read_text()
    assert "> ⚠️ claude の最終取り込みは 2026-05-01（65日前）" in md
    assert "> ⚠️ gemini のエクスポートは一度も取り込まれていません" in md
    assert "chatgpt の最終取り込み" not in md
    assert out["status"] == "created"


def test_stale_uses_conversation_recency_for_uploads(env):
    """/api/import records uploads as source='upload', so a fresh archive
    must also count as fresh via its newest conversation (real-DB finding:
    import_runs alone flags every manual source as never-imported)."""
    db, vault = env
    make_conv(db, "c1", title="今週のChatGPT会話")  # source=chatgpt, THIS_WEEK
    from app.deliver import weekly_review
    stale = weekly_review.stale_exports(now=NOW)
    by_source = {s["source"]: s for s in stale}
    assert "chatgpt" not in by_source            # fresh via conversations
    assert by_source["claude"]["last"] is None    # truly no data
    assert by_source["gemini"]["last"] is None


def test_empty_archive_still_produces_review(env):
    db, vault = env
    from app.deliver import weekly_review
    out = weekly_review.run(now=NOW, llm=fixture_llm([DRAFT]))
    assert out["status"] == "created"
    md = (weekly_dir(vault) / "2026-W27.md").read_text()
    assert "_なし_" in md
    assert "_今週の活動に結びつく過去の項目は見つかりませんでした。_" in md


def test_target_exists_checks_containment(env, tmp_path):
    """target_exists must not treat an escaping base dir as a normal miss."""
    db, vault = env
    outside = tmp_path / "outside"
    outside.mkdir()
    (vault / "External Brain" / "40 Reviews").mkdir(parents=True)
    (vault / "External Brain" / "40 Reviews" / "Weekly").symlink_to(outside)
    from app.deliver import obsidian_writer
    with pytest.raises(obsidian_writer.ObsidianWriteError):
        obsidian_writer.target_exists("weekly", "2026-W27.md")
