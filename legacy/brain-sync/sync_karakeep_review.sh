#!/bin/bash
set -euo pipefail

cd "$(dirname "$0")"

if [[ ! -f config.env ]]; then
  echo "config.env がありません" >&2
  exit 1
fi

# shellcheck disable=SC1091
source config.env

: "${KARAKEEP_URL:?KARAKEEP_URL が設定されていません}"
: "${OBSIDIAN_VAULT:?OBSIDIAN_VAULT が設定されていません}"
: "${OBSIDIAN_EXTERNAL_BRAIN_DIR:?OBSIDIAN_EXTERNAL_BRAIN_DIR が設定されていません}"

TARGET_DIR="$OBSIDIAN_VAULT/$OBSIDIAN_EXTERNAL_BRAIN_DIR/90 Auto"
TARGET_FILE="$TARGET_DIR/karakeep-to-review.md"
TEMP_FILE="$(mktemp)"

if [[ ! -d "$TARGET_DIR" ]]; then
  echo "出力先がありません: $TARGET_DIR" >&2
  exit 1
fi

KARAKEEP_API_KEY="$(
  security find-generic-password \
    -a "$USER" \
    -s "brain-sync-karakeep" \
    -w
)"

JSON="$(
  curl -fsS \
    -H "Authorization: Bearer $KARAKEEP_API_KEY" \
    -H "Accept: application/json" \
    "$KARAKEEP_URL/api/v1/bookmarks?limit=100&sortOrder=desc"
)"

COUNT="$(
  jq '
    [
      .bookmarks[]
      | select(any(.tags[]?; .name == "to-review"))
    ]
    | length
  ' <<<"$JSON"
)"

{
  cat <<EOF2
---
source: karakeep
type: review-index
generated: $(date '+%Y-%m-%d %H:%M:%S')
item_count: $COUNT
---

# Karakeep — 要レビュー

Karakeepで \`to-review\` タグを付けた項目の自動一覧です。

EOF2

  jq -r '
    .bookmarks[]
    | select(any(.tags[]?; .name == "to-review"))
    | (
        .title
        // .content.title
        // .content.url
        // "無題"
      ) as $title
    | (
        .content.url
        // ""
      ) as $url
    | (
        [.tags[]?.name] | join(", ")
      ) as $tags
    | (
        .createdAt
        // ""
      ) as $created
    | "## " + $title + "\n\n"
      + (if $url != "" then "- URL: " + $url + "\n" else "" end)
      + "- Karakeep ID: `" + .id + "`\n"
      + "- 保存日時: " + $created + "\n"
      + "- タグ: " + $tags + "\n"
      + "\n"
      + "- [ ] 内容を確認\n"
      + "- [ ] Zoteroへ昇格\n"
      + "- [ ] Obsidianのテーマへ反映\n"
      + "\n---\n"
  ' <<<"$JSON"
} > "$TEMP_FILE"

mv "$TEMP_FILE" "$TARGET_FILE"

echo "Created: $TARGET_FILE"
echo "Items: $COUNT"
