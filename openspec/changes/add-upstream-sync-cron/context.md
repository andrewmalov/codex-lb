# upstream-sync context

## Purpose

`codex-lb` is forked at `andrewmalov/codex-lb` from `Soju06/codex-lb`. The fork carries local customizations — Claude OAuth pool, deployment experiments, fixes — that exist only on the fork. Upstream moves independently and the operator does not want to track those changes by hand.

`upstream-sync` is the operational answer: a delegated, agent-driven sync that pulls `upstream/main` into the fork's `main` via a sync-PR (so CI and `@codex review` stay in the loop) and reports every run as a GitHub issue for audit. When the merge touches files the fork itself customizes, the agent stops and asks for human help instead of guessing.

## Decisions

- **Sync via PR, not direct push.** The sync-PR goes through the project's normal merge gates (`CI Required`, `@codex review`, `mergeable=CLEAN`) per `.github/CONTRIBUTING.md`. This keeps the operator's CI / review invariants intact and avoids a special-case "auto-bypass" branch that future maintainers would have to remember.
- **`git merge --no-ff upstream/main` into a `sync/upstream-<date>` branch.** Rebase rewrites SHAs of the fork's existing commits, which breaks any open PRs that reference them. `merge --no-ff` is safe, idempotent, and easy to bisect. Squash loses provenance of upstream changes.
- **Worktree isolation.** The merge happens in `/tmp/codex-lb-sync-<date>` so the operator's normal working copy is never disturbed mid-edit. If the operator is in the middle of a feature branch with dirty files, the sync still runs.
- **Lock file `/tmp/codex-lb-sync.lock`.** Prevents overlapping runs (e.g. operator manually triggers while launchd is also firing). Second run exits with `skipped_locked` and no side-effects.
- **Conflict classification is dynamic, not hard-coded.** The agent computes "the fork's customization surface" as `git diff --name-only origin/main..HEAD` — i.e. paths the fork has touched beyond upstream — and stops if any of those paths also appear in the upstream delta. Hard-coding a list (e.g. `app/modules/claude/`) would rot the moment the fork changes its shape.
- **`openspec/specs/` is sync'd one-to-one.** This project is OpenSpec-first; if upstream changes a spec, the fork needs the same spec or downstream validation will drift. We do NOT re-import upstream changes as local OpenSpec changes — that would double the documentation surface — but the agent MUST validate (`openspec validate --specs`) that nothing local now points at a spec upstream archived.
- **Reports are GitHub issues, not Telegram or external.** The operator explicitly chose this channel. Issues provide audit, label-based filtering (`sync-report`, `sync-blocker`, `sync-failure`), and persistence without standing up new infrastructure.
- **No telemetry, no external services.** The sync is self-contained: git, gh, Claude Code, GitHub. No ANTHROPIC_API_KEY calls, no Slack webhooks, no Tailnet.
- **launchd template is provided but not required.** Operators on Linux can wire the same `scripts/sync_upstream.sh` into systemd or crontab. The wrapper is the only artifact cron-like schedulers actually need.
- **Skill is the SSOT, not the script.** `SKILL.md` is what `claude -p` reads; if behavior changes, the spec + skill change together, and the change is reviewable as an OpenSpec change. The shell wrapper stays thin and stays shellcheck-clean.

## Constraints

- `GITHUB_TOKEN` (PAT) is the only secret. It MUST come from the environment (per `CLAUDE.md`) and MUST NOT appear in any PR/issue body or any committed file.
- The agent MUST NOT bypass merge gates. The sync-PR is reviewed exactly like any other PR; the operator may merge when green.
- Sync runs are bounded to `main`. Feature branches (`feature/*`, `fix/*`, `release/*`) are explicitly out of scope — the operator rebases or merges them into `main` on their own schedule.
- The agent MUST stay read-only on application code outside the merge itself. It cannot, for example, "fix" the proxy or "rewrite" a CI workflow during sync. Sync is sync.
- A failed sync MUST NOT leave a sync-PR open. Either the PR is opened only when the merge is clean, or no PR is opened and the issue is the only artifact.

## Failure modes

| Failure | Detected by | Agent action |
|---------|-------------|--------------|
| `GITHUB_TOKEN` missing or expired | preflight `gh auth status` | exit 1, `sync-failure` issue |
| Wrong branch (`feature/test-server-cd`) | preflight `git rev-parse --abbrev-ref HEAD` | exit 1, `sync-failure` issue naming the branch |
| Dirty working tree | preflight `git status --porcelain` | exit 1, `sync-failure` issue with status dump |
| Lock-file collision | preflight `/tmp/codex-lb-sync.lock` exists | exit 3, no side-effects |
| `upstream` remote wrong URL | preflight `git remote get-url upstream` | exit 1, `sync-failure` issue |
| Transient network failure on `fetch` | preflight retry budget exhausted | exit 1, `sync-failure` issue |
| Conflict in fork-customized file | merge classification | exit 2, `sync-blocker` issue with patch.diff |
| Sync-PR already exists for today | `gh pr list --head sync/upstream-<date>` | `gh pr edit ... --body ...` instead of duplicate |
| CI fails on sync-PR | post-push monitoring is out of scope; flagged in sync-report issue | leave to operator; no auto-retry |
| Operator manually merged an unrelated PR mid-sync | race; lock-file mitigates | operator-visible in next sync-report diffstat |

## Example run

This is a single successful auto-merged sync, condensed from a representative timeline.

```
# Operator (one-time setup)
$ scripts/setup_upstream_remote.sh
upstream → https://github.com/Soju06/codex-lb.git (added)

# launchd fires at 02:30 local
$ /Users/me/.local/bin/sync_upstream.sh
[2026-07-07 02:30:01] preflight: branch=main clean=true upstream=ok auth=ok lock=free
[2026-07-07 02:30:03] fetch upstream/main: ok (8 commits ahead of origin/main)
[2026-07-07 02:30:03] worktree: /tmp/codex-lb-sync-2026-07-07
[2026-07-07 02:30:04] merge --no-ff upstream/main: clean
[2026-07-07 02:30:05] push origin sync/upstream-2026-07-07: ok
[2026-07-07 02:30:07] gh pr create: chore(sync): upstream/main as of 2026-07-07
[2026-07-07 02:30:09] gh issue create: sync-upstream report 2026-07-07 (label sync-report)
[2026-07-07 02:30:09] worktree cleanup: ok
[2026-07-07 02:30:09] lock released: ok
exit 0

# Operator sees in fork's issue list
# Issue #42 [sync-report] sync-upstream report 2026-07-07
#   Status: auto_merged
#   PR: https://github.com/andrewmalov/codex-lb/pull/43
#   Diffstat: 8 files changed, 142 insertions(+), 27 deletions(-)
#   Commits:
#     abc1234 fix(proxy): handle model fetch timeouts
#     def5678 feat(api-keys): provider_scope end-to-end
#     ...
#   Action: review PR #43, merge once CI green and @codex review clean
```

A blocked run looks like this:

```
[2026-07-08 02:30:01] preflight: ok
[2026-07-08 02:30:03] fetch upstream/main: ok (3 commits ahead)
[2026-07-08 02:30:04] worktree: /tmp/codex-lb-sync-2026-07-08
[2026-07-08 02:30:05] merge --no-ff upstream/main: CONFLICT in app/modules/proxy/load_balancer.py
[2026-07-08 02:30:05] classification: blocked (file is in fork customization surface)
[2026-07-08 02:30:06] worktree cleanup: ok
[2026-07-08 02:30:06] lock released: ok
exit 2

# Operator sees
# Issue #44 [sync-blocker, sync-report] sync-upstream stopped_blocker 2026-07-08
#   Conflicting files:
#     - app/modules/proxy/load_balancer.py
#   Patch:
#     ```diff
#     ... git diff of the merge attempt ...
#     ```
#   Reproduction:
#     cd /tmp && git clone https://github.com/andrewmalov/codex-lb.git sync-repro
#     cd sync-repro && git remote add upstream https://github.com/Soju06/codex-lb.git
#     git fetch upstream main
#     git checkout -b sync/upstream-2026-07-08 origin/main
#     git merge --no-ff upstream/main
#     # resolve manually, push, open a new sync-PR
```

## Related

- `.github/CONTRIBUTING.md` — merge gates and OpenSpec-first convention this capability respects.
- `CLAUDE.md` — defines `GITHUB_TOKEN` as the auth env var and the project's OpenSpec-first workflow.
- `openspec/specs/release-automation` — release-please reads Conventional Commits from the merged sync-PRs.
- `openspec/changes/add-claude-oauth-pool/` — the most recent fork customization whose protection motivates the dynamic customization-surface check.