#!/usr/bin/env bash
# Add the Soju06/codex-lb upstream remote for the andrewmalov/codex-lb fork.
# Idempotent: safe to re-run; updates nothing if already configured.
#
# Usage: scripts/setup_upstream_remote.sh
#
# Exit codes:
#   0 - upstream remote configured (added or already correct)
#   1 - prerequisite failure (not a git repo, missing git, etc.)
#
# This script does NOT push or fetch. It only inspects / mutates the
# local `upstream` remote in `.git/config`. Re-run any time; no side
# effects when the remote already points at the expected URL.

set -euo pipefail

EXPECTED_URL="https://github.com/Soju06/codex-lb.git"

# Resolve repo root (the parent of .git, wherever that may be).
if ! git rev-parse --show-toplevel >/dev/null 2>&1; then
  echo "error: not inside a git repository" >&2
  exit 1
fi
REPO_ROOT="$(git rev-parse --show-toplevel)"

current_url="$(git remote get-url upstream 2>/dev/null || true)"

if [[ -z "${current_url}" ]]; then
  git remote add upstream "${EXPECTED_URL}"
  echo "added upstream -> ${EXPECTED_URL}"
  exit 0
fi

if [[ "${current_url}" != "${EXPECTED_URL}" ]]; then
  echo "upstream already exists but points to a different URL:" >&2
  echo "  current:   ${current_url}" >&2
  echo "  expected:  ${EXPECTED_URL}" >&2
  echo "refusing to overwrite. If this is intentional, run:" >&2
  echo "  git remote set-url upstream ${EXPECTED_URL}" >&2
  exit 1
fi

echo "upstream already configured: ${current_url}"
exit 0
