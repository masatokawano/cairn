#!/bin/bash
set -euo pipefail

cd "$(dirname "$0")"

if [[ ! -f config.env ]]; then
  echo "config.env がありません" >&2
  exit 1
fi

# shellcheck disable=SC1091
source config.env

if [[ -z "${KARAKEEP_URL:-}" ]]; then
  echo "KARAKEEP_URL が設定されていません" >&2
  exit 1
fi

KARAKEEP_API_KEY="$(
  security find-generic-password \
    -a "$USER" \
    -s "brain-sync-karakeep" \
    -w
)"

curl -fsS \
  -H "Authorization: Bearer $KARAKEEP_API_KEY" \
  -H "Accept: application/json" \
  "$KARAKEEP_URL/api/v1/bookmarks?limit=100&sortOrder=desc" \
| jq '
  [
    .bookmarks[]
    | select(any(.tags[]?; .name == "to-review"))
    | {
        id,
        title,
        url: .content.url,
        tags: [.tags[].name],
        createdAt
      }
  ]
'
