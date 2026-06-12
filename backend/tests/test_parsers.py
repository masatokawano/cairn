import json
import os

from app.parsers import chatgpt, claude_cli, claude_export, codex_cli, gemini, parse_upload

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def load(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return f.read()


def test_chatgpt_parse():
    data = json.loads(load("chatgpt_sample.json"))
    assert chatgpt.looks_like(data)
    result = chatgpt.parse(data)
    assert len(result.conversations) == 1  # empty conv and bad entry dropped
    conv = result.conversations[0]
    assert conv.source == "chatgpt"
    assert conv.source_id == "abc-123"
    assert conv.title == "Python sorting question"
    assert [m.role for m in conv.messages] == ["user", "assistant"]
    assert "安定ソート" in conv.messages[1].text
    assert conv.messages[0].created_at.startswith("2024-")
    assert any("entry 2" in w for w in result.warnings)


def test_claude_export_parse():
    data = json.loads(load("claude_sample.json"))
    assert claude_export.looks_like(data)
    assert not chatgpt.looks_like(data)
    result = claude_export.parse(data)
    assert len(result.conversations) == 1
    conv = result.conversations[0]
    assert conv.source == "claude"
    assert conv.title == "FTS5の使い方"
    assert len(conv.messages) == 3  # content-blocks msg, plus old text-only msg
    assert conv.messages[1].text == "trigramトークナイザを使うと部分一致検索が可能です。"
    assert conv.messages[2].text == "古い形式のtextのみのメッセージ"


def test_gemini_parse():
    data = json.loads(load("gemini_sample.json"))
    assert gemini.looks_like(data)
    result = gemini.parse(data)
    assert len(result.conversations) == 2  # Maps record excluded
    first, second = result.conversations
    assert first.messages[0].text == "東京の明日の天気を教えて"
    assert len(first.messages) == 1  # no response in record
    assert len(second.messages) == 2  # subtitles treated as response
    assert second.messages[1].role == "assistant"
    assert any("Maps" in w for w in result.warnings)


def test_claude_cli_parse():
    content = load("claude_cli_sample.jsonl")
    result = claude_cli.parse_file("/tmp/sess-1.jsonl", content)
    assert len(result.conversations) == 1
    conv = result.conversations[0]
    assert conv.source == "claude_cli"
    assert conv.source_id == "sess-1"
    assert conv.title == "ビルドエラーの調査"  # from summary line
    assert conv.meta["cwd"] == "/Users/masato/workspace/demo"
    texts = [m.text for m in conv.messages]
    assert texts == [
        "swiftcのビルドエラーを直して",
        "エラーログを確認します。",
        "原因はリンクエラーでした。-framework Cocoa を追加してください。",
    ]  # sidechain, tool_result, command wrapper, thinking all excluded
    assert any("bad JSON" in w for w in result.warnings)


def test_codex_cli_parse():
    content = load("codex_cli_sample.jsonl")
    result = codex_cli.parse_file("/tmp/rollout.jsonl", content)
    assert len(result.conversations) == 1
    conv = result.conversations[0]
    assert conv.source == "codex_cli"
    assert conv.source_id == "codex-sess-1"
    assert conv.meta["cwd"] == "/Users/masato/workspace/demo"
    texts = [m.text for m in conv.messages]
    # developer role, AGENTS.md/environment_context noise excluded
    assert texts == [
        "VSCodeでCodex拡張の使い方を教えて",
        "サイドバーから Codex: Sign In を実行して認証してください。",
    ]
    assert conv.title.startswith("VSCodeでCodex拡張")


def test_codex_cli_ide_wrapper_unwrapped():
    line = json.dumps({
        "timestamp": "2025-09-26T02:38:00.000Z",
        "type": "response_item",
        "payload": {"type": "message", "role": "user", "content": [
            {"type": "input_text",
             "text": "# Context from my IDE setup:\n\n## My request for Codex:\nvscodeにcodex拡張をインストールしました。"}
        ]},
    })
    result = codex_cli.parse_file("/tmp/r.jsonl", line)
    assert result.conversations[0].messages[0].text == "vscodeにcodex拡張をインストールしました。"


def test_claude_cli_command_wrapper_excluded():
    lines = "\n".join([
        json.dumps({"type": "user", "isSidechain": False, "uuid": "u1", "sessionId": "s",
                    "timestamp": "2026-01-01T00:00:00Z",
                    "message": {"role": "user", "content": "<command-message>init</command-message>"}}),
        json.dumps({"type": "user", "isSidechain": False, "uuid": "u2", "sessionId": "s",
                    "timestamp": "2026-01-01T00:00:01Z",
                    "message": {"role": "user", "content": "Caveat: The messages below were generated..."}}),
        json.dumps({"type": "user", "isSidechain": False, "uuid": "u3", "sessionId": "s",
                    "timestamp": "2026-01-01T00:00:02Z",
                    "message": {"role": "user", "content": "本物の質問"}}),
    ])
    result = claude_cli.parse_file("/tmp/s.jsonl", lines)
    assert [m.text for m in result.conversations[0].messages] == ["本物の質問"]
    assert result.conversations[0].title == "本物の質問"


def test_parse_upload_detects_each_format():
    for name, source in [
        ("chatgpt_sample.json", "chatgpt"),
        ("claude_sample.json", "claude"),
        ("gemini_sample.json", "gemini"),
    ]:
        result = parse_upload(name, load(name).encode("utf-8"))
        assert result.conversations[0].source == source


def test_parse_upload_zip():
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("conversations.json", load("chatgpt_sample.json"))
    result = parse_upload("export.zip", buf.getvalue())
    assert result.conversations[0].source == "chatgpt"


def test_parse_upload_unknown_format():
    import pytest

    from app.parsers import UnknownFormatError

    with pytest.raises(UnknownFormatError):
        parse_upload("x.json", b'[{"foo": 1}]')
