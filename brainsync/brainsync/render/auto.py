"""`90 Auto/` 向け自動一覧のレンダラ。

出力構造は統合前の実装（sync_*.py / sync_karakeep_review.sh）と互換を保つ。
見出し・項目・フィールド・frontmatter を変えるときは golden テストを更新する（T3）。
外部由来テキストはすべて escape_inline() を通す（不変条件 4）。
"""

from __future__ import annotations

from datetime import datetime

from brainsync.connectors import karakeep, zotero
from brainsync.markdown import escape_inline

OBSIDIAN_LOOKBACK_DAYS = 30


def render_cairn_recent(items: list[dict], generated: datetime) -> str:
    lines = [
        "---",
        "source: cairn",
        "type: recent-conversations-index",
        f"generated: {generated:%Y-%m-%d %H:%M:%S%z}",
        "lookback_days: 7",
        f"item_count: {len(items)}",
        "---",
        "",
        "# Cairn — 直近7日間の会話",
        "",
        "Cairnに保存された最近の生成AI対話の自動一覧です。",
        "",
    ]

    for item in items:
        title = escape_inline(item.get("title")) or "無題"
        source = escape_inline(item.get("source") or "unknown")
        conversation_id = int(item["id"])
        updated_at = escape_inline(item.get("updated_at"))
        message_count = int(item.get("message_count", 0))
        project_dir = (item.get("meta") or {}).get("cwd")

        lines.extend(
            [
                f"## {title}",
                "",
                f"- Source: `{source}`",
                f"- Cairn ID: `{conversation_id}`",
                f"- 更新日時: {updated_at}",
                f"- メッセージ数: {message_count}",
            ]
        )

        if project_dir:
            lines.append(f"- Project: `{escape_inline(project_dir)}`")

        lines.extend(
            [
                "",
                "- [ ] 内容を確認",
                "- [ ] Obsidianへ反映",
                "- [ ] 未解決課題を抽出",
                "",
                "---",
                "",
            ]
        )

    return "\n".join(lines)


def render_karakeep_review(items: list[dict], generated: datetime) -> str:
    # 旧 bash 実装は date '+%Y-%m-%d %H:%M:%S'（タイムゾーンなし）だった。互換維持。
    lines = [
        "---",
        "source: karakeep",
        "type: review-index",
        f"generated: {generated:%Y-%m-%d %H:%M:%S}",
        f"item_count: {len(items)}",
        "---",
        "",
        "# Karakeep — 要レビュー",
        "",
        "Karakeepで `to-review` タグを付けた項目の自動一覧です。",
        "",
    ]

    for item in items:
        title = escape_inline(karakeep.bookmark_title(item))
        url = escape_inline(karakeep.bookmark_url(item))
        tags = ", ".join(
            escape_inline(tag) for tag in karakeep.bookmark_tags(item)
        )
        bookmark_id = escape_inline(item.get("id"))
        created = escape_inline(item.get("createdAt"))

        lines.extend([f"## {title}", ""])
        if url:
            lines.append(f"- URL: {url}")
        lines.extend(
            [
                f"- Karakeep ID: `{bookmark_id}`",
                f"- 保存日時: {created}",
                f"- タグ: {tags}",
                "",
                "- [ ] 内容を確認",
                "- [ ] Zoteroへ昇格",
                "- [ ] Obsidianのテーマへ反映",
                "",
                "---",
                "",
            ]
        )

    return "\n".join(lines)


def render_zotero_recent(items: list[dict], generated: datetime) -> str:
    lines = [
        "---",
        "source: zotero",
        "type: recent-items-index",
        f"generated: {generated:%Y-%m-%d %H:%M:%S%z}",
        "lookback_days: 7",
        f"item_count: {len(items)}",
        "---",
        "",
        "# Zotero — 直近7日間の資料",
        "",
        "Zoteroで最近追加または更新された資料の自動一覧です。",
        "",
    ]

    for item in items:
        data = item["data"]
        key = escape_inline(item.get("key"))

        title = escape_inline(data.get("title")) or "無題"
        item_type = escape_inline(data.get("itemType")) or "unknown"
        modified = escape_inline(data.get("dateModified"))
        url_value = escape_inline(data.get("url"))
        doi = escape_inline(data.get("DOI"))

        creators = [
            escape_inline(zotero.creator_name(creator))
            for creator in data.get("creators", [])
            if zotero.creator_name(creator)
        ]
        creator_text = ", ".join(creators[:5])

        tags = [
            escape_inline(tag.get("tag"))
            for tag in data.get("tags", [])
            if (tag.get("tag") or "").strip()
        ]

        lines.extend(
            [
                f"## {title}",
                "",
                f"- 種別: `{item_type}`",
                f"- Zotero Key: `{key}`",
                f"- 更新日時: {modified}",
            ]
        )

        if creator_text:
            lines.append(f"- 著者: {creator_text}")
        if doi:
            lines.append(f"- DOI: `{doi}`")
        if url_value:
            lines.append(f"- URL: {url_value}")
        if tags:
            lines.append(f"- タグ: {', '.join(tags)}")

        lines.extend(
            [
                "",
                "- [ ] 内容を確認",
                "- [ ] Obsidianのテーマへ反映",
                "- [ ] Cairnの関連対話を探す",
                "",
                "---",
                "",
            ]
        )

    return "\n".join(lines)


def render_obsidian_context(
    themes: list[dict],
    projects: list[dict],
    generated: datetime,
) -> str:
    lines = [
        "---",
        "source: obsidian",
        "type: current-context-index",
        f"generated: {generated:%Y-%m-%d %H:%M:%S%z}",
        f"lookback_days: {OBSIDIAN_LOOKBACK_DAYS}",
        f"theme_count: {len(themes)}",
        f"project_count: {len(projects)}",
        "---",
        "",
        "# Obsidian — 現在の理解",
        "",
        "最近更新されたテーマノートとプロジェクトノートの自動一覧です。",
        "",
        "## Themes",
        "",
    ]

    # link は Vault 内の自ノートへの wikilink（自分の管理下）なのでエスケープしない。
    if themes:
        for item in themes:
            local_time = item["modified"].astimezone()
            lines.append(f"- {item['link']} — 更新 {local_time:%Y-%m-%d %H:%M}")
    else:
        lines.append("_最近更新されたテーマノートはありません。_")

    lines.extend(["", "## Projects", ""])

    if projects:
        for item in projects:
            local_time = item["modified"].astimezone()
            lines.append(f"- {item['link']} — 更新 {local_time:%Y-%m-%d %H:%M}")
    else:
        lines.append("_最近更新されたプロジェクトノートはありません。_")

    lines.extend(
        [
            "",
            "## Review",
            "",
            "- [ ] 現在のテーマを確認",
            "- [ ] 進行中プロジェクトを確認",
            "- [ ] 今週の資料・対話との関連を確認",
            "",
        ]
    )

    return "\n".join(lines)


def render_connection_test(created: datetime) -> str:
    lines = [
        "---",
        "source: brain-sync",
        "type: connection-test",
        f"created: {created:%Y-%m-%d %H:%M:%S}",
        "---",
        "",
        "# Brain Sync 接続テスト",
        "",
        "KarakeepとObsidianの接続準備が完了しました。",
        "",
    ]
    return "\n".join(lines)


__all__ = [
    "render_cairn_recent",
    "render_karakeep_review",
    "render_zotero_recent",
    "render_obsidian_context",
    "render_connection_test",
    "OBSIDIAN_LOOKBACK_DAYS",
]
