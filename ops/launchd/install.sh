#!/bin/bash
# Materialise the com.masato.cairn.* LaunchAgents from the templates in this
# directory (M3, DESIGN.md §5.7). Renders the plists and copies them into
# ~/Library/LaunchAgents — it does NOT load them: bootstrapping/unloading is
# an explicit human step (AGENTS.md invariant 8). Print-only guidance at the
# end tells you what to run.
#
# Usage:
#   KARAKEEP_URL=https://keep.example.com ZOTERO_USER_ID=12345 \
#     ./install.sh [vault_path] [external_brain_dir]
#
#   vault_path         default: $HOME/Obsidian
#   external_brain_dir default: External Brain
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
BACKEND_DIR="$(cd "$HERE/../../backend" && pwd)"
LOG_DIR="$HOME/Library/Logs/cairn"
AGENT_DIR="$HOME/Library/LaunchAgents"

KARAKEEP_URL="${KARAKEEP_URL:?KARAKEEP_URL is required (e.g. https://keep.example.com)}"
ZOTERO_USER_ID="${ZOTERO_USER_ID:?ZOTERO_USER_ID is required}"
OBSIDIAN_VAULT="${1:-$HOME/Obsidian}"
EXTERNAL_BRAIN_DIR="${2:-External Brain}"

[ -x "$BACKEND_DIR/bin/cairn" ] || { echo "cairn CLI not found at $BACKEND_DIR/bin/cairn" >&2; exit 1; }
[ -d "$OBSIDIAN_VAULT" ] || { echo "vault not found: $OBSIDIAN_VAULT" >&2; exit 1; }

mkdir -p "$LOG_DIR" "$AGENT_DIR"

render() {
    sed -e "s|{{BACKEND_DIR}}|$BACKEND_DIR|g" \
        -e "s|{{LOG_DIR}}|$LOG_DIR|g" \
        -e "s|{{KARAKEEP_URL}}|$KARAKEEP_URL|g" \
        -e "s|{{ZOTERO_USER_ID}}|$ZOTERO_USER_ID|g" \
        -e "s|{{OBSIDIAN_VAULT}}|$OBSIDIAN_VAULT|g" \
        -e "s|{{EXTERNAL_BRAIN_DIR}}|$EXTERNAL_BRAIN_DIR|g" \
        "$1"
}

for name in com.masato.cairn.sync com.masato.cairn.weekly; do
    render "$HERE/$name.plist.template" > "$AGENT_DIR/$name.plist"
    echo "installed: $AGENT_DIR/$name.plist"
done

cat <<'EOF'

Next (run these yourself — loading agents is an explicit step):
  launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/com.masato.cairn.sync.plist
  launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/com.masato.cairn.weekly.plist

To retire the legacy brain-sync agents, see ops/launchd/MIGRATION.md.
EOF
