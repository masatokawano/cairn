#!/bin/bash
set -euo pipefail

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

cd "/Users/masato/workspace/brain-sync"

# shellcheck disable=SC1091
source config.env

if ! curl -fsS \
  --max-time 5 \
  "$CAIRN_URL/api/stats" \
  >/dev/null; then
  echo "Cairn API is unavailable; keeping the previous Obsidian file."
  exit 0
fi

./sync_cairn_recent.py
