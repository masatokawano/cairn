#!/bin/bash
set -euo pipefail

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

cd "/Users/masato/workspace/brain-sync"

# まずKarakeepの最新一覧を更新
./sync_karakeep_review.sh

# その週のレビューがなければ作成
./create_weekly_review.sh
