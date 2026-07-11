# upstream-sync Specification (delta)

## ADDED Requirements

### Requirement: Sync trigger

The system SHALL expose an operator-invocable sync entry point under the repository root. The entry point SHALL be invokable as:

- A slash command `/sync-upstream` from any Claude Code session in the repository, AND
- A non-interactive shell invocation `scripts/sync_upstream.sh` that wraps `claude -p "/sync-upstream"` with `--output-format json --permission-mode acceptEdits`.

The wrapper SHALL capture stdout and stderr into `logs/sync_upstream_<YYYY-MM-DD>.log` where `<YYYY-MM-DD>` is the local date of the run.

#### Scenario: Operator triggers sync manually

- **GIVEN** the operator is in a Claude Code session at the repo root with `GITHUB_TOKEN` available
- **WHEN** the operator runs `/sync-upstream`
- **THEN** the agent performs the steps in the `Skill` requirement and produces a structured result with `status` ∈ `{"up_to_date","auto_merged","stopped_blocker","preflight_failed"}`

#### Scenario: Scheduler triggers sync

- **GIVEN** `scripts/sync_upstream.sh` is executed by the operator's scheduler with `GITHUB_TOKEN` in the environment
- **WHEN** the wrapper runs the agent
- **THEN** the agent behaves identically to the `/sync-upstream` slash command and writes a log line per step

### Requirement: Skill behavior — preflight

On every invocation, the agent MUST perform preflight before any fetch. Preflight MUST verify ALL of the following, in order, and abort on the first failure:

1. `gh auth status` reports a logged-in user OR `git config --get-url.origin` is reachable with the credentials available (so the agent can distinguish "no token" from "expired token").
2. The current branch is `main`. If not, the agent MUST NOT switch branches; it MUST abort and report `preflight_failed` with reason `wrong_branch`.
3. The working tree is clean (`git status --porcelain` is empty). If dirty, the agent MUST abort with `preflight_failed` reason `dirty_tree`.
4. The lock file `/tmp/codex-lb-sync.lock` does not exist. If it does, the agent MUST exit with status `skipped_locked` without any side-effects.
5. The remote `upstream` exists and points to `https://github.com/Soju06/codex-lb.git`. If missing, the agent MUST add it (`git remote add upstream https://github.com/Soju06/codex-lb.git`); if present but pointing elsewhere, the agent MUST refuse and report `preflight_failed` reason `upstream_remote_wrong_url`.
6. `git fetch upstream main` succeeds. On failure, the agent MUST retry up to 3 times with exponential backoff (initial delay 5s, multiplier 2) and then abort with `preflight_failed` reason `upstream_fetch_failed`.

#### Scenario: Preflight detects wrong branch

- **GIVEN** the operator is on `feature/test-server-cd`
- **WHEN** the agent runs preflight
- **THEN** it aborts with `preflight_failed` reason `wrong_branch` and files one sync-report issue whose body names the current branch

#### Scenario: Preflight detects dirty tree

- **GIVEN** the working tree contains unstaged or staged changes
- **WHEN** the agent runs preflight
- **THEN** it aborts with `preflight_failed` reason `dirty_tree` and files one sync-report issue whose body includes `git status --porcelain` output

#### Scenario: Preflight adds missing upstream remote

- **GIVEN** the `upstream` remote is not configured
- **WHEN** the agent runs preflight
- **THEN** it adds `upstream` → `https://github.com/Soju06/codex-lb.git` and continues to step 5

#### Scenario: Preflight retries a transient fetch failure

- **GIVEN** the first `git fetch upstream main` fails with a network error
- **WHEN** the agent retries with exponential backoff
- **THEN** the second attempt succeeds and the run continues normally

### Requirement: Skill behavior — fetch and compare

After preflight passes, the agent MUST:

1. Compute `diff_stat` = `git diff --shortstat origin/main..upstream/main` and `new_commits` = `git log --oneline origin/main..upstream/main`.
2. If `diff_stat` is empty (no new commits on upstream), the agent MUST exit cleanly with status `up_to_date`. In that case the agent MUST NOT open a sync-PR and MUST NOT file a sync-report issue.

#### Scenario: Upstream is up-to-date

- **GIVEN** `origin/main` already contains every commit on `upstream/main`
- **WHEN** the agent compares the two
- **THEN** the run exits with status `up_to_date` and produces no PR or issue

### Requirement: Skill behavior — isolated worktree

When new commits exist upstream, the agent MUST perform the merge in a `git worktree` at `/tmp/codex-lb-sync-<YYYY-MM-DD>` on a fresh branch `sync/upstream-<YYYY-MM-DD>`, leaving the operator's working copy untouched.

#### Scenario: Worktree isolates the merge

- **GIVEN** the operator has uncommitted edits on `main`
- **WHEN** the agent creates the sync worktree and merges
- **THEN** the operator's working copy remains untouched and the merge happens only inside `/tmp/codex-lb-sync-<date>`

#### Scenario: Worktree is cleaned up

- **WHEN** the sync run finishes (success, blocker, or preflight failure after the worktree was created)
- **THEN** the agent removes the worktree with `git worktree remove --force` and clears `/tmp/codex-lb-sync.lock`

### Requirement: Skill behavior — merge classification

Inside the sync worktree, the agent MUST run `git merge --no-ff upstream/main` and classify the result:

- `clean` — no conflicts, OR conflicts only in files outside the fork's customization surface (defined below). The agent resolves such conflicts in favor of `upstream` and continues.
- `blocked` — conflicts in any file that appears in `git diff --name-only origin/main..HEAD` of the fork (the fork's customization surface) AND that file is also changed in the upstream delta. The agent MUST stop and report `stopped_blocker`.

The "fork customization surface" is defined as the set of paths changed by any commit on `origin/main` that is not on `upstream/main`. The agent MUST compute this set dynamically per run; the set is NOT hard-coded.

#### Scenario: Conflict in code the fork also touches

- **GIVEN** upstream changes `app/modules/proxy/load_balancer.py` AND the fork's `main` has prior commits touching `app/modules/proxy/load_balancer.py` (e.g. claude-oauth-pool)
- **WHEN** `git merge --no-ff upstream/main` reports a conflict in that file
- **THEN** the agent reports `stopped_blocker` and does NOT push or open a sync-PR

#### Scenario: Conflict only in upstream-only files

- **GIVEN** upstream changes `docs/CHANGELOG.md` (a file the fork has not touched) and `openspec/specs/api-keys/spec.md` (a file the fork has not touched)
- **WHEN** `git merge --no-ff upstream/main` succeeds without conflict, OR has conflicts only in those files
- **THEN** the agent resolves in favor of upstream and continues to push + open a sync-PR

### Requirement: Skill behavior — push and sync-PR

When the merge resolves cleanly, the agent MUST:

1. Push `sync/upstream-<YYYY-MM-DD>` to `origin` (`git push -u origin sync/upstream-<YYYY-MM-DD>`).
2. Open a sync-PR with `gh pr create` whose:
   - title is `chore(sync): upstream/main as of <YYYY-MM-DD>`,
   - base is `main`, head is `sync/upstream-<YYYY-MM-DD>`,
   - body MUST include a diffstat and a bulleted list of the merged commits with SHAs and Conventional Commits subjects.
3. If a sync-PR for `<YYYY-MM-DD>` already exists, the agent MUST update its body instead of opening a duplicate (`gh pr edit ... --body ...`).

The sync-PR MUST then go through the project's normal merge gates (CI green, `@codex review` clean, `mergeable=CLEAN`) per `.github/CONTRIBUTING.md`. The agent MUST NOT bypass these gates or fast-forward `main` directly.

#### Scenario: Sync-PR body lists the merged commits

- **GIVEN** a clean merge with three upstream commits
- **WHEN** the agent opens the sync-PR
- **THEN** the PR body contains `git log --oneline origin/main..upstream/main` output (or its post-merge equivalent) with one bullet per commit

#### Scenario: Idempotent re-run on the same day

- **GIVEN** a sync-PR `chore(sync): upstream/main as of 2026-07-07` already exists
- **WHEN** the agent runs again on the same date after another commit lands upstream
- **THEN** the agent updates the existing PR's body and commits rather than creating a second PR

### Requirement: Skill behavior — audit issue

Every sync run MUST produce exactly one GitHub issue in the fork (`andrewmalov/codex-lb`) that serves as the audit trail for that run, using `gh issue create`:

- Title: `sync-upstream report <YYYY-MM-DD>` on success/auto-merge, OR `sync-upstream <status> <YYYY-MM-DD>` otherwise.
- Labels: `sync-report` on every report; additionally `sync-blocker` when the run stopped on conflict or `sync-failure` when preflight failed.
- Body MUST include:
  - Status (`up_to_date`, `auto_merged`, `stopped_blocker`, `preflight_failed`).
  - Run start / end timestamps.
  - Upstream HEAD SHA (`upstream/main` post-fetch).
  - Local `origin/main` HEAD SHA at run start.
  - The diffstat of the merged range.
  - Link to the sync-PR (or note that none was opened).
  - When `stopped_blocker`: list of conflicting files, the `patch.diff`, and reproduction commands the operator can use to finish the merge manually.
  - When `preflight_failed`: the specific reason and the captured stderr of the failing command.

The agent MUST NOT include `GITHUB_TOKEN`, `Authorization` headers, or any other secret value in any issue or PR body.

#### Scenario: Stopped run files sync-blocker issue with patch

- **GIVEN** the agent classified the merge as `blocked`
- **WHEN** it prepares the audit issue
- **THEN** the issue's body contains a fenced code block with the `git diff` of the merge conflict (`patch.diff`), lists every conflicting path, and links reproduction commands

#### Scenario: Clean run files sync-report issue

- **GIVEN** the agent opened a sync-PR
- **WHEN** it files the audit issue
- **THEN** the issue links to the PR and contains the diffstat and commit list

#### Scenario: Secrets are never written to issue bodies

- **WHEN** the agent runs any branch of the skill
- **THEN** no issue or PR body contains the literal string `GITHUB_TOKEN`, an `Authorization:` header, or any token value present in the agent's environment

### Requirement: Exit codes

The wrapper MUST exit with one of the following codes:

- `0` — `up_to_date` (no upstream changes) OR `auto_merged` (sync-PR opened).
- `1` — `preflight_failed` (auth, branch, dirty tree, lock, fetch failures).
- `2` — `stopped_blocker` (conflicts touching the fork's customization surface). Operator action required.
- `3` — `skipped_locked` (another sync was running).

#### Scenario: Exit code reflects status

- **WHEN** any sync run completes
- **THEN** the wrapper's exit code matches the classification above

### Requirement: Out-of-scope guarantees

The agent MUST NOT:

- Push directly to `main` on `origin` (push targets are `sync/upstream-*` branches only).
- Rebase or rewrite history on any branch.
- Modify the operator's feature branches (`feature/*`, `fix/*`, `release/*`).
- Delete or archive anything in `openspec/changes/`.
- Touch proxy, account, dashboard, or release-automation code; this capability is read-only on application code outside of the merge itself.
- Open sync-PRs whose base is anything other than `main` on the fork.

#### Scenario: Push target restriction

- **WHEN** the agent runs any `git push` command
- **THEN** the only allowed refspec is `HEAD:sync/upstream-<YYYY-MM-DD>` to `origin`
