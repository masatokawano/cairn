"""Export Markdown tests (P1-G): 1 conversation should render as a readable
Markdown section with source / dates visible, and the shared filter layer from
P1-F should keep working (sanity check only — filters are exercised by
test_export.py)."""
import importlib
import io
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


def make_conv(source="chatgpt", source_id="c1", title="エクスポート対象",
              updated="2025-01-01T00:10:00Z", texts=None):
    from app.parsers.base import ParsedConversation, ParsedMessage
    texts = texts or [("user", "質問本文"), ("assistant", "回答本文")]
    return ParsedConversation(
        source=source, source_id=source_id, title=title,
        messages=[
            ParsedMessage(role=r, text=t, created_at=f"2025-01-01T00:0{i}:00Z")
            for i, (r, t) in enumerate(texts)
        ],
        created_at="2025-01-01T00:00:00Z", updated_at=updated,
    )


def _dump(db, **filters) -> str:
    buf = io.StringIO()
    n = db.export_markdown(buf, **filters)
    out = buf.getvalue()
    # Returned count matches the number of conversation headings rendered.
    assert out.count("\n# ") + (1 if out.startswith("# ") else 0) == n
    return out


def test_markdown_single_conversation_is_readable(db):
    db.upsert_conversations([make_conv(
        title="今日の質問",
        texts=[("user", "あいうえお"), ("assistant", "回答です")],
    )])
    md = _dump(db)
    # Heading carries the title.
    assert md.startswith("# 今日の質問")
    # source / source_id / dates are visible at the top so the reader can tell
    # where this conversation came from.
    assert "- source: chatgpt" in md
    assert "- source_id: c1" in md
    assert "- created_at: 2025-01-01T00:00:00Z" in md
    assert "- updated_at: 2025-01-01T00:10:00Z" in md
    # Each message becomes its own role-tagged section with its timestamp,
    # body preserved verbatim.
    assert "## user" in md
    assert "## assistant" in md
    assert "あいうえお" in md
    assert "回答です" in md


def test_markdown_separates_multiple_conversations(db):
    db.upsert_conversations([
        make_conv("chatgpt", "a", title="一つ目"),
        make_conv("claude_cli", "b", title="二つ目"),
    ])
    md = _dump(db)
    assert md.count("# 一つ目") == 1
    assert md.count("# 二つ目") == 1
    # A horizontal rule separates conversations so a reader / Obsidian can
    # split on it; only ONE separator for two conversations.
    assert md.count("\n---\n") == 1


def test_markdown_filter_passes_through(db):
    # P1-F already exercises the filter layer thoroughly — confirm here that
    # export_markdown wires through to the same filter so behavior is shared.
    db.upsert_conversations([
        make_conv("chatgpt", "a", title="A"),
        make_conv("claude_cli", "b", title="B"),
    ])
    md = _dump(db, source="claude_cli")
    assert "# B" in md
    assert "# A" not in md


def test_admin_export_markdown_to_file(db, tmp_path, capsys):
    from app import admin
    importlib.reload(admin)
    db.upsert_conversations([make_conv("chatgpt", "a", title="抜き出し")])
    out = str(tmp_path / "dump.md")
    rc = admin.main(["export-markdown", "--out", out])
    assert rc == 0
    assert os.path.exists(out)
    # Contains plaintext conversation data — locked down to 0600 like backup/export-jsonl.
    assert oct(os.stat(out).st_mode & 0o777) == "0o600"
    with open(out, encoding="utf-8") as f:
        body = f.read()
    assert "# 抜き出し" in body
    # Status line lands on stderr (the file is the artifact, not stdout).
    assert "1" in capsys.readouterr().err


def test_admin_export_markdown_to_stdout(db, capsys):
    from app import admin
    importlib.reload(admin)
    db.upsert_conversations([make_conv("chatgpt", "a", title="標準出力")])
    rc = admin.main(["export-markdown"])
    assert rc == 0
    cap = capsys.readouterr()
    # Markdown on stdout, status on stderr — keeps pipes/redirects clean.
    assert "# 標準出力" in cap.out
    assert "1" in cap.err
