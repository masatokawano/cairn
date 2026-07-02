#!/bin/bash
set -euo pipefail

cd "$(dirname "$0")"

if [[ ! -f config.env ]]; then
  echo "config.env がありません" >&2
  exit 1
fi

# shellcheck disable=SC1091
source config.env

: "${CAIRN_URL:?CAIRN_URL が設定されていません}"

JSON="$(
  curl -fsS \
    -H "Accept: application/json" \
    "$CAIRN_URL/api/conversations?limit=3"
)"

jq -e '
  (.results | type == "array")
  and
  (all(.results[];
    (.id | type == "number")
    and (.source | type == "string")
    and (.title | type == "string")
    and (.updated_at | type == "string")
    and (.message_count | type == "number")
  ))
' <<<"$JSON" >/dev/null

COUNT="$(jq '.results | length' <<<"$JSON")"

echo "Cairn API connection OK"
echo "Retrieved conversations: $COUNT"

jq -r '
  .results[]
  | "- [\(.source)] #\(.id) \(.title) — \(.message_count) messages, updated \(.updated_at)"
' <<<"$JSON"
