#!/bin/bash
set -euo pipefail

cd "$(dirname "$0")"

if [[ ! -f config.env ]]; then
  echo "config.env がありません" >&2
  exit 1
fi

# shellcheck disable=SC1091
source config.env

: "${ZOTERO_USER_ID:?ZOTERO_USER_ID が設定されていません}"
: "${ZOTERO_API_URL:?ZOTERO_API_URL が設定されていません}"

ZOTERO_API_KEY="$(
  security find-generic-password \
    -a "$USER" \
    -s "brain-sync-zotero" \
    -w
)"

JSON="$(
  curl -fsS \
    -H "Zotero-API-Key: $ZOTERO_API_KEY" \
    -H "Zotero-API-Version: 3" \
    -H "Accept: application/json" \
    "$ZOTERO_API_URL/users/$ZOTERO_USER_ID/items/top?limit=3&sort=dateModified&direction=desc"
)"

jq -e '
  type == "array"
  and
  all(.[];
    (.key | type == "string")
    and (.version | type == "number")
    and (.data | type == "object")
  )
' <<<"$JSON" >/dev/null

COUNT="$(jq 'length' <<<"$JSON")"

echo "Zotero API connection OK"
echo "Retrieved items: $COUNT"

jq -r '
  .[]
  | "- [\(.data.itemType)] \(.data.title // "無題") — key=\(.key), modified=\(.data.dateModified)"
' <<<"$JSON"
