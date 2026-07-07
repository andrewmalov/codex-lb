#!/usr/bin/env bash
# Installs the upstream sync launchd agent.
# Usage: ./install_launchd.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOURCE_PLIST="$REPO_ROOT/scripts/launchd.example.plist"
TARGET_DIR="$HOME/Library/LaunchAgents"
TARGET_PLIST="$TARGET_DIR/com.andrewmalov.codex-lb.sync-upstream.plist"

if [[ ! -f "$SOURCE_PLIST" ]]; then
    echo "ERROR: launchd template not found at $SOURCE_PLIST" >&2
    exit 1
fi

echo "This script will install the upstream sync launchd agent."
echo ""
echo "Before continuing:"
echo "  1. Edit $SOURCE_PLIST and update:"
echo "     - ProgramArguments path to '$REPO_ROOT/scripts/sync_upstream.sh'"
echo "     - StandardOutPath / StandardErrorPath paths"
echo "  2. Ensure GITHUB_TOKEN is exported in your shell profile:"
echo "     export GITHUB_TOKEN='ghs_...'"
echo ""
read -r "Ready to install to $TARGET_PLIST? [y/N] " CONFIRM
if [[ "$CONFIRM" != "y" ]]; then
    echo "Aborted."
    exit 0
fi

mkdir -p "$TARGET_DIR"
cp "$SOURCE_PLIST" "$TARGET_PLIST"
launchctl load -w "$TARGET_PLIST"
echo "Installed. To uninstall: launchctl unload $TARGET_PLIST && rm $TARGET_PLIST"