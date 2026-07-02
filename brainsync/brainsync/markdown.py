"""markdown 出力の共通部。外部由来テキストの無害化を一手に引き受ける。

会話タイトル・ブックマークタイトル・タグ等の外部由来テキストは信頼しない
（責務分界の不変条件 4）。markdown へ流す前に必ず escape_inline() を通す。
"""

from __future__ import annotations


def escape_inline(value: str | None) -> str:
    """外部由来テキストを 1 行のインラインテキストとして無害化する。

    - 改行・連続空白は単一スペースに畳む（見出し・リスト構造の注入防止）
    - バッククォートは ' に置換（code span 内でもバックスラッシュが効かず
      span を破って脱出できるため、エスケープではなく置換で潰す）
    - `[` `]` `|` はバックスラッシュエスケープ（wikilink・テーブル注入防止）
    - 行頭 `#` はエスケープ（見出し化防止）
    """
    text = " ".join((value or "").split())
    text = text.replace("`", "'")
    for ch in "[]|":
        text = text.replace(ch, "\\" + ch)
    if text.startswith("#"):
        text = "\\" + text
    return text
