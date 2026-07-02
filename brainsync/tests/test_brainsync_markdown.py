from __future__ import annotations

from brainsync.markdown import escape_inline


def test_collapses_newlines_and_whitespace():
    assert escape_inline("line1\nline2\r\n  line3\t x") == "line1 line2 line3 x"


def test_escapes_leading_heading_marker():
    assert escape_inline("# not a heading").startswith("\\#")
    # 行頭以外の # はそのまま
    assert escape_inline("issue #42") == "issue #42"


def test_escapes_brackets_and_pipe():
    assert escape_inline("[[wikilink]] and |pipe|") == (
        "\\[\\[wikilink\\]\\] and \\|pipe\\|"
    )


def test_backtick_cannot_break_out_of_code_span():
    escaped = escape_inline("evil ` breakout")
    assert "`" not in escaped


def test_none_and_empty():
    assert escape_inline(None) == ""
    assert escape_inline("") == ""
    assert escape_inline("   ") == ""


def test_multiline_heading_injection_is_neutralized():
    escaped = escape_inline("title\n# injected heading\n- [ ] injected task")
    assert "\n" not in escaped
