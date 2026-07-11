# Proposal: upstream-sync

## Why

`codex-lb` is forked at `andrewmalov/codex-lb` from `Soju06/codex-lb`. The fork carries an active customization surface (claude-oauth-pool, fixes, deploy experiments) that lives only on the fork. Upstream moves independently — bug fixes, OpenSpec changes, dependency bumps, Helm chart updates — and the fork currently has no automated way to pick those up.

Manually running `git fetch upstream && git merge upstream/main` every few days is friction the operator does not want to carry. We want a delegated, agent-driven sync that:

- Detects new commits on `upstream/main`,
- Brings them into the fork's `main` via a sync-PR (so CI + `@codex review` stay in the loop),
- Reports every run as a GitHub issue (audit trail + signal-on-failure),
- Stops and asks for human help the moment the merge touches files the fork itself customizes.

## What Changes

- Add a Claude Code skill `sync-upstream` that, given a prompt, performs preflight → fetch → classify → auto-merge or stop → push sync branch → open sync-PR → file issue report. The skill is the operational SSOT for sync behavior.
- Add a thin shell wrapper `scripts/sync_upstream.sh` that exposes `GITHUB_TOKEN` (per CLAUDE.md "GitHub auth … is available via env vars") and calls `claude -p "/sync-upstream"` with `--output-format json --permission-mode acceptEdits`.
- Add a launchd plist template `scripts/launchd.example.plist` plus an installer `scripts/install_launchd.sh` so the operator can schedule the wrapper at most daily, with logs under `logs/sync_upstream_YYYY-MM-DD.log`. The wrapper is **not** opinionated about the scheduler — crontab or systemd timers are equally valid.
- Add an idempotent `scripts/setup_upstream_remote.sh` that creates the `upstream` remote (`https://github.com/Soju06/codex-lb.git`) if it does not already exist. The sync skill also performs this check on every run as a self-healing fallback.
- Add a new capability `upstream-sync` whose `spec.md` is the normative SSOT for sync behavior (what the agent MUST do on every run) and whose `context.md` records decisions, failure modes, and one concrete worked example.
- Add the unit-test and dry-run integration-test scaffolding required to verify the spec without hitting the real upstream.

## Capabilities

### New Capabilities

- `upstream-sync`: automated, agent-driven sync of `upstream/main` into the fork via a sync-PR. Read-only on everything except `origin/main` and `origin/sync/upstream-*`. Always files an audit issue per run.

### Modified Capabilities

None. This change adds a new capability; it does not modify the behavior of existing proxy, dashboard, accounts, or deployment surfaces.

## Non-goals

- Auto-merging the sync-PR into the fork's `main`. The sync-PR still goes through the project's merge gates (CI green, `@codex review` clean, `mergeable=CLEAN`) per `.github/CONTRIBUTING.md`.
- Rebasing or rewriting history of the fork's `main`. Sync uses `git merge --no-ff upstream/main` into a `sync/upstream-<date>` branch, then opens a PR; the fork's `main` is never fast-forwarded directly.
- Syncing feature branches. Only `main` is in scope. Feature branches merge from `main` on the operator's schedule.
- Mandatory launchd. The scheduler is the operator's choice; the wrapper is the only required artifact for cron/launchd/systemd.
- Telegram / external notifications. Reports are GitHub issues in the fork, which is the operator's preferred channel.

## Impact

- **Repo state**: Two new directories (`openspec/changes/add-upstream-sync-cron/`, `openspec/specs/upstream-sync/` once archived), one new directory of sync scripts (`scripts/sync_upstream.{sh,launchd.example.plist,setup_upstream_remote.sh,install_launchd.sh}`).
- **Working copy during sync**: A `git worktree` at `/tmp/codex-lb-sync-<date>` holds the sync branch so the operator's normal working copy is never disturbed mid-edit.
- **Auth**: `GITHUB_TOKEN` (PAT) is the only secret needed. Read from the shell environment, not committed, not logged in plaintext. Per `CLAUDE.md`, the env var is already wired.
- **PR / Issue side-effects**: Each sync run either opens one sync-PR (auto-merged cleanly) or one `sync-blocker` issue (stopped on conflict) + one `sync-report` issue, plus possibly updates an existing sync-PR's body instead of doubling up.
- **No runtime impact**: This change does not modify the proxy, dashboard, accounts, or any code path that handles requests. It is purely an out-of-band maintainer tool.
- **OpenSpec hygiene**: The `add-upstream-sync-cron` change folder follows the project's OpenSpec-first convention. `spec.md` contains only testable requirements; `context.md` carries purpose, rationale, decisions, failure modes, and an example.