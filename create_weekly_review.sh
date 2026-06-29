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

AUTO_DIR="$OBSIDIAN_VAULT/$OBSIDIAN_EXTERNAL_BRAIN_DIR/90 Auto"
TARGET_DIR="$OBSIDIAN_VAULT/$OBSIDIAN_EXTERNAL_BRAIN_DIR/40 Reviews/Weekly"

KARAKEEP_FILE="$AUTO_DIR/karakeep-to-review.md"
CAIRN_FILE="$AUTO_DIR/cairn-recent.md"

# テスト時だけ BRAIN_SYNC_WEEK=2099-W01 のように上書き可能
YEAR_WEEK="${BRAIN_SYNC_WEEK:-$(date '+%G-W%V')}"
TARGET_FILE="$TARGET_DIR/$YEAR_WEEK.md"

mkdir -p "$TARGET_DIR"

if [[ -e "$TARGET_FILE" ]]; then
  echo "Weekly review already exists: $TARGET_FILE"
  exit 0
fi

extract_items() {
  local source_file="$1"

  if [[ ! -f "$source_file" ]]; then
    echo "_データファイルがまだありません。_"
    return
  fi

  # YAML frontmatter、文書タイトル、説明文を除き、
  # 最初の項目見出し（##）以降だけを出力する
  awk '
    BEGIN {
      in_frontmatter = 0
      body_started = 0
    }

    NR == 1 && $0 == "---" {
      in_frontmatter = 1
      next
    }

    in_frontmatter && $0 == "---" {
      in_frontmatter = 0
      next
    }

    in_frontmatter {
      next
    }

    /^## / {
      body_started = 1
    }

    body_started {
      print
    }
  ' "$source_file"
}

{
  cat <<EOF2
---
type: weekly-external-brain-review
week: $YEAR_WEEK
created: $(date '+%Y-%m-%d %H:%M:%S')
status: open
sources:
  - karakeep
  - cairn
---

# External Brain Weekly Review — $YEAR_WEEK

## 今週の処理方針

- [ ] Karakeepの保存資料を確認する
- [ ] Cairnの重要な対話を確認する
- [ ] Zoteroへ昇格する根拠資料を選ぶ
- [ ] Obsidianへ反映する着想・結論を選ぶ
- [ ] 未解決課題を整理する
- [ ] レビューを完了する

---

# Karakeep：発見したもの

EOF2

  extract_items "$KARAKEEP_FILE"

  cat <<'EOF2'

---

# Cairn：考えた過程

EOF2

  extract_items "$CAIRN_FILE"

  cat <<'EOF2'

---

# 今週の統合メモ

## 繰り返し現れたテーマ

## 新しく得た着想

## 根拠資料として残すもの

## 過去の考えから変化した点

## 未解決の問い

## 来週行うこと

EOF2
} > "$TARGET_FILE"

echo "Created: $TARGET_FILE"
