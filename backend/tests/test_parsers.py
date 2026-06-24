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
    # Fixture has no attachments → attachments stays empty (additive).
    assert all(m.attachments == [] for m in conv.messages)


def test_claude_export_attachments_and_files():
    """Real Claude.ai export carries uploads at the message level, not in
    content blocks: `attachments[]` with extracted text, and `files[]` with
    UUID references whose bytes are not in the export."""
    import hashlib as _h

    data = [{
        "uuid": "conv-1",
        "name": "添付ありの会話",
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:01:00Z",
        "chat_messages": [
            {
                "uuid": "m1",
                "sender": "human",
                "text": "このファイルの要約をお願い",
                "content": [{"type": "text", "text": "このファイルの要約をお願い"}],
                "created_at": "2026-01-01T00:00:00Z",
                "attachments": [{
                    "file_name": "paste.txt",
                    "file_size": 12,
                    "file_type": "txt",
                    "extracted_content": "hello world!",
                }],
                "files": [{"file_uuid": "f-uuid-1", "file_name": "image.png"}],
            },
            {
                # text-empty turn that is "just an upload" — must still be kept.
                "uuid": "m2",
                "sender": "human",
                "text": "",
                "content": [],
                "created_at": "2026-01-01T00:00:30Z",
                "attachments": [],
                "files": [{"file_uuid": "f-uuid-2", "file_name": "doc.pdf"}],
            },
            {
                "uuid": "m3",
                "sender": "assistant",
                "text": "了解。",
                "content": [{"type": "text", "text": "了解。"}],
                "created_at": "2026-01-01T00:01:00Z",
                "attachments": [],
                "files": [],
            },
        ],
    }]
    result = claude_export.parse(data)
    assert result.warnings == []
    conv = result.conversations[0]
    assert [m.text for m in conv.messages] == ["このファイルの要約をお願い", "", "了解。"]
    # m1: one extracted attachment + one file ref.
    m1_atts = conv.messages[0].attachments
    assert len(m1_atts) == 2
    ext = next(a for a in m1_atts if a.extracted_text is not None)
    assert ext.source_ref == "paste.txt"
    assert ext.mime == "text/plain"
    assert ext.size == 12
    assert ext.extracted_text == "hello world!"
    assert ext.hash == _h.sha256(b"hello world!").hexdigest()
    fref = next(a for a in m1_atts if a.extracted_text is None)
    assert fref.source_ref == "f-uuid-1"
    assert fref.hash is None
    # m2: text was empty but the file-only turn is preserved.
    assert conv.messages[1].attachments[0].source_ref == "f-uuid-2"
    # m3: no attachments.
    assert conv.messages[2].attachments == []


def test_gemini_parse():
    data = json.loads(load("gemini_sample.json"))
    assert gemini.looks_like(data)
    result = gemini.parse(data)
    # 4 records → 2 conversations: Maps excluded, "フィードバック" excluded
    assert len(result.conversations) == 2
    first, second = result.conversations

    # Weather Q with an HTML response (safeHtmlItem is now used).
    assert first.messages[0].text == "東京の明日の天気を教えて"
    assert len(first.messages) == 2
    assert first.messages[1].role == "assistant"
    assert "晴れ時々曇り" in first.messages[1].text
    assert "22°C" in first.messages[1].text  # block tags became newlines
    assert first.messages[0].attachments == []
    assert first.messages[1].attachments == []

    # Image-input + generated-image Q.
    assert second.messages[0].text == "この画像の問題を解いて"
    user_atts = second.messages[0].attachments
    asst_atts = second.messages[1].attachments
    # user: IMG_1245 (from subtitles + imageFile + attachedFiles, deduped).
    # assistant: answer-gen.png (only the <img> in safeHtmlItem).
    assert [a.source_ref for a in user_atts] == ["IMG_1245-abc.jpeg"]
    assert [a.source_ref for a in asst_atts] == ["answer-gen.png"]
    # No bytes passed in this test → hash/size stay None, mime is best-effort.
    assert user_atts[0].mime == "image/jpeg"
    assert asst_atts[0].mime == "image/png"
    assert user_atts[0].hash is None and user_atts[0].size is None
    assert "3π cm²" in second.messages[1].text

    assert any("Maps" in w for w in result.warnings)


def test_gemini_attachments_hashed_when_zip_provided():
    """When parse() receives the surrounding ZIP's bytes, image attachments
    get sha256 + size; without it they record only source_ref."""
    import hashlib as _h

    data = json.loads(load("gemini_sample.json"))
    img_bytes = b"\x89PNG\r\n\x1a\n" + b"fake png body" * 32
    jpg_bytes = b"\xff\xd8\xff\xe0" + b"fake jpg body" * 16
    attachments_map = {
        "Takeout/My Activity/Gemini Apps/IMG_1245-abc.jpeg": jpg_bytes,
        "Takeout/My Activity/Gemini Apps/answer-gen.png": img_bytes,
    }
    result = gemini.parse(data, attachments=attachments_map)
    second = result.conversations[1]
    user_att = second.messages[0].attachments[0]
    asst_att = second.messages[1].attachments[0]
    assert user_att.size == len(jpg_bytes)
    assert user_att.hash == _h.sha256(jpg_bytes).hexdigest()
    assert asst_att.size == len(img_bytes)
    assert asst_att.hash == _h.sha256(img_bytes).hexdigest()


def test_gemini_attachment_only_user_message_preserved():
    """An attachment-only turn (no prompt text) is still kept."""
    data = [{
        "header": "Gemini アプリ",
        "title": "送信したメッセージ: ",
        "time": "2026-04-07T07:18:15.144Z",
        "products": ["Gemini アプリ"],
        "subtitles": [
            {"name": "添付ファイル 1 件"},
            {"name": "-  IMG.jpeg", "url": "IMG-xyz.jpeg"},
        ],
        "imageFile": "IMG-xyz.jpeg",
        "attachedFiles": ["IMG-xyz.jpeg"],
        "safeHtmlItem": [{"html": "<p>Answer</p>"}],
    }]
    result = gemini.parse(data)
    assert len(result.conversations) == 1
    conv = result.conversations[0]
    assert conv.messages[0].text == ""
    assert [a.source_ref for a in conv.messages[0].attachments] == ["IMG-xyz.jpeg"]
    assert conv.messages[1].text == "Answer"


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
