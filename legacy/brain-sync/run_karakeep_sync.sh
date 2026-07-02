#!/bin/bash
set -euo pipefail

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

cd "/Users/masato/workspace/brain-sync"

./sync_karakeep_review.sh
