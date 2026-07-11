#!/usr/bin/env bash
# Non-interactive wrapper around the sync-upstream Claude Code skill.
#
# Exports GITHUB_TOKEN (PAT) from the operator's environment into the
# Claude invocation, sets cwd to the repo root, runs
# `claude -p "/sync-upstream"` with `--output-format json` and
# `--permission-mode acceptEdits`, and tees stdout/stderr to
# `logs/sync_upstream_YYYY-MM-DD.log`.
#
# The wrapper is intentionally scheduler-agnostic: it works identically
# when launched by launchd, cron, systemd, or a manual shell. The skill
# itself is the only place that knows how to perform a sync.
#
# Usage:
#   scripts/sync_upstream.sh            # run a real sync
#   scripts/sync_upstream.sh --dry-run  # plan + classify only, no push/PR/issue
#
# Exit codes:
#   0 - skill exited 0 (up_to_date or auto_merged)
#   1 - preflight_failed
#   2 - stopped_blocker (fork-customized conflict, audit issue filed)
#   3 - skipped_locked (concurrent run detected, no side-effects)
#   4 - claude CLI missing or crashed
#
# Required env vars:
#   GITHUB_TOKEN - PAT with `repo` + `workflow` scopes
#
# Required tools:
#   git, jq (for parsing the machine-readable result line), claude

set -euo pipefail

if [[ "${1:-}" == "--dry-run" ]]; then
  PROMPT_ARGS="/sync-upstream --dry-run"
else
  PROMPT_ARGS="/sync-upstream"
fi

# --- Preflight: git repo + claude + jq ---------------------------------------
if ! git rev-parse --show-toplevel >/dev/null 2>&1; then
  echo "error: not inside a git repository" >&2
  exit 4
fi

if ! command -v claude >/dev/null 2>&1; then
  echo "error: claude CLI not found in PATH; install Claude Code first" >&2
  exit 4
fi

if ! command -v jq >/dev/null 2>&1; then
  echo "error: jq not found in PATH; install jq (e.g. `brew install jq`)" >&2
  exit 4
fi

if [[ -z "${GITHUB_TOKEN:-}" ]]; then
  echo "error: GITHUB_TOKEN is not set; refusing to run" >&2
  exit 1
fi

# --- Set cwd to repo root and prepare log ------------------------------------
REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "${REPO_ROOT}"

LOG_DIR="${REPO_ROOT}/logs"
mkdir -p "${LOG_DIR}"
LOG_FILE="${LOG_DIR}/sync_upstream_$(date +%F).log"

# --- Run the skill -----------------------------------------------------------
# Tee to log; surface only the trailing JSON result line on stdout so the
# wrapper and any caller (e.g. launchd) can parse status without scanning
# every line of agent output.
{
  echo "=== sync_upstream run at $(date -Iseconds) ==="
  echo "cwd: ${REPO_ROOT}"
  echo "git describe: $(git describe --always --dirty 2>/dev/null || echo unknown)"
  echo "branch: $(git rev-parse --abbrev-ref HEAD)"
  echo "upstream remote: $(git remote get-url upstream 2>/dev/null || echo NONE)"
  echo "--- claude invocation ---"
} >>"${LOG_FILE}"

set +e
GITHUB_TOKEN="${GITHUB_TOKEN}" \
  claude -p "${PROMPT_ARGS}" \
    --output-format json \
    --permission-mode acceptEdits \
    >>"${LOG_FILE}" 2>&1
claude_rc=$?
set -e

# Append the same exit metadata to the log for ops postmortems.
{
  echo "--- claude exit code: ${claude_rc} ---"
} >>"${LOG_FILE}"

# Parse the machine-readable result line emitted by the skill on its last
# stdout line. The wrapper's own callers (launchd, cron) read this and
# translate to a small set of exit codes.
status_line="$(grep -E '^\{"status":' "${LOG_FILE}" | tail -n1 || true)"
if [[ -z "${status_line}" ]]; then
  # No structured result -> treat the claude CLI's exit code as authoritative.
  case "${claude_rc}" in
    0) exit_code=0 ;;
    *) exit_code=4 ;;
  esac
  echo "warning: no structured result line in log; falling back to claude exit (${claude_rc})" >&2
  exit "${exit_code}"
fi

status="$(printf '%s' "${status_line}" | jq -r '.status // empty')"
case "${status}" in
  up_to_date|auto_merged)
    exit_code=0
    ;;
  preflight_failed)
    exit_code=1
    ;;
  stopped_blocker)
    exit_code=2
    ;;
  skipped_locked)
    exit_code=3
    ;;
  *)
    echo "warning: unrecognized status '${status}'; mapping to claude-side error" >&2
    exit_code=4
    ;;
esac

# Echo the result line so callers / launchd can read it on stdout.
echo "${status_line}"
exit "${exit_code}"
