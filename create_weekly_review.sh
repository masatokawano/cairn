#!/bin/bash
set -euo pipefail

cd "$(dirname "$0")"

if [[ ! -f config.env ]]; then
  echo "config.env がありません" >&2
  exit 1
fi

# shellcheck disable=SC1091
source config.env

: "${OBSIDIAN_VAULT:?OBSIDIAN_VAULT が設定されていません}"
: "${OBSIDIAN_EXTERNAL_BRAIN_DIR:?OBSIDIAN_EXTERNAL_BRAIN_DIR が設定されていません}"

SOURCE_FILE="$OBSIDIAN_VAULT/$OBSIDIAN_EXTERNAL_BRAIN_DIR/90 Auto/karakeep-to-review.md"
TARGET_DIR="$OBSIDIAN_VAULT/$OBSIDIAN_EXTERNAL_BRAIN_DIR/40 Reviews/Weekly"

YEAR_WEEK="$(date '+%G-W%V')"
TARGET_FILE="$TARGET_DIR/$YEAR_WEEK.md"

if [[ ! -f "$SOURCE_FILE" ]]; then
  echo "元ファイルがありません: $SOURCE_FILE" >&2
  exit 1
fi

mkdir -p "$TARGET_DIR"

if [[ -e "$TARGET_FILE" ]]; then
  echo "Weekly review already exists: $TARGET_FILE"
  exit 0
fi

{
  cat <<EOF2
---
type: weekly-external-brain-review
week: $YEAR_WEEK
created: $(date '+%Y-%m-%d %H:%M:%S')
status: open
---

# External Brain Weekly Review — $YEAR_WEEK

## 今週の処理方針

- [ ] Karakeep項目を確認する
- [ ] Zoteroへ昇格する資料を選ぶ
- [ ] Obsidianへ反映する着想を選ぶ
- [ ] レビューを完了する

---

EOF2

  # 元ファイルのYAML frontmatterと最初の見出し・説明を除き、
  # 各項目部分を週次レビューへコピーする
  awk '
    BEGIN { frontmatter = 0; separators = 0; body = 0 }

    NR == 1 && $0 == "---" {
      frontmatter = 1
      next
    }

    frontmatter && $0 == "---" {
      frontmatter = 0
      next
    }

    frontmatter {
      next
    }

    /^## / {
      body = 1
    }

    body {
      print
    }
  ' "$SOURCE_FILE"
} > "$TARGET_FILE"

echo "Created: $TARGET_FILE"
