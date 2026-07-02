#!/bin/bash
set -euo pipefail

cd "$(dirname "$0")"

if [[ ! -f config.env ]]; then
  echo "config.env がありません" >&2
  exit 1
fi

# shellcheck disable=SC1091
source config.env

if [[ -z "${OBSIDIAN_VAULT:-}" ]]; then
  echo "OBSIDIAN_VAULT が設定されていません" >&2
  exit 1
fi

if [[ -z "${OBSIDIAN_EXTERNAL_BRAIN_DIR:-}" ]]; then
  echo "OBSIDIAN_EXTERNAL_BRAIN_DIR が設定されていません" >&2
  exit 1
fi

TARGET_DIR="$OBSIDIAN_VAULT/$OBSIDIAN_EXTERNAL_BRAIN_DIR/90 Auto"
TARGET_FILE="$TARGET_DIR/brain-sync-test.md"

if [[ ! -d "$TARGET_DIR" ]]; then
  echo "対象ディレクトリが見つかりません: $TARGET_DIR" >&2
  exit 1
fi

cat > "$TARGET_FILE" <<EOF2
---
source: brain-sync
type: connection-test
created: $(date '+%Y-%m-%d %H:%M:%S')
---

# Brain Sync 接続テスト

KarakeepとObsidianの接続準備が完了しました。
EOF2

echo "Created: $TARGET_FILE"
