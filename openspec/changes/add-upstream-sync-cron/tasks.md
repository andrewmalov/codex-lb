# Tasks

## 1. OpenSpec scaffolding

- [ ] 1.1 Confirm `openspec/changes/add-upstream-sync-cron/{proposal.md,tasks.md,specs/upstream-sync/spec.md,context.md}` are committed as a coherent change
- [ ] 1.2 Run `openspec validate --change add-upstream-sync-cron --strict` and resolve any failures before opening the PR

## 2. Sync scripts

- [ ] 2.1 Implement `scripts/setup_upstream_remote.sh` (idempotent: adds `upstream` → `https://github.com/Soju06/codex-lb.git` if missing; prints current upstream URL on re-run)
- [ ] 2.2 Implement `scripts/sync_upstream.sh` that exports `GITHUB_TOKEN` from env, sets `cwd` to the repo root, invokes `claude -p "/sync-upstream"` with `--output-format json --permission-mode acceptEdits`, and routes stdout/stderr to `logs/sync_upstream_$(date +%F).log`
- [ ] 2.3 Make both scripts executable (`chmod +x`) and lint them with `shellcheck` if available; add to `Makefile` `lint` target if practical
- [ ] 2.4 Add `scripts/launchd.example.plist` template (commented-out `GITHUB_TOKEN` example, `StartCalendarInterval` at the operator's preferred cadence, log paths matching the wrapper)
- [ ] 2.5 Add `scripts/install_launchd.sh` that copies the example plist into `~/Library/LaunchAgents/`, prompts for `GITHUB_TOKEN`, then `launchctl load -w` it. Print the matching `launchctl unload` and uninstall instructions

## 3. Skill

- [ ] 3.1 Create `.claude/skills/sync-upstream/SKILL.md` containing the step-by-step behavior:
  - preflight (auth, branch, clean tree, lock file, upstream remote existence),
  - fetch + diff against `origin/main`,
  - early-exit on no-op,
  - isolated `git worktree` work,
  - merge classification (auto-resolvable vs. touched-by-fork → stop),
  - push + `gh pr create` (auto-resolved path),
  - patch + `gh issue create --label sync-blocker` (stopped path),
  - always-on `gh issue create --label sync-report` final audit issue,
  - worktree teardown and lock-file release
- [ ] 3.2 Ensure the skill never logs `GITHUB_TOKEN` value and never includes token strings in PR or issue bodies
- [ ] 3.3 Confirm the skill is invokable both as a slash command (`/sync-upstream`) and via `claude -p "/sync-upstream"` so the wrapper and the operator's ad-hoc use share one prompt source

## 4. Tests

- [ ] 4.1 Add `tests/unit/test_upstream_sync_skill_layout.py` that asserts `SKILL.md` exists, the required sections (preflight, fetch, merge, PR, issue, fail-modes) are present, and the file does not contain the literal token strings from the test fixture
- [ ] 4.2 Add `tests/integration/test_sync_upstream_dryrun.py` that spins up a local bare "fake-upstream" repo, plants known commits, and asserts each scenario from context.md:
  - no-op (upstream unchanged) → exit 0, no PR, no issue,
  - clean fast-forward merge → exit 0, sync-PR body matches diffstat,
  - merge conflict in a file customized by the fork → exit 2, `sync-blocker` issue text mentions the conflicting paths, and `patch.diff` is present,
  - preflight failure (no upstream remote, no GITHUB_TOKEN) → exit 1, `sync-upstream preflight failure` issue,
  - lock-file collision → second concurrent run exits without side-effects.
- [ ] 4.3 Add a `make test-sync-dryrun` target that runs `tests/integration/test_sync_upstream_dryrun.py` and any future sync regression in isolation
- [ ] 4.4 Run `make ci` locally and confirm all gates pass before opening the PR

## 5. Dry-run + verification

- [ ] 5.1 Run `scripts/sync_upstream.sh --dry-run` against the real upstream on a sandbox branch and observe a successful sync-PR dry-run with no destructive effects
- [ ] 5.2 Open the sync-PR for real, confirm CI green + `@codex review` clean, then close it without merging (it is a verification artifact, not a real sync)
- [ ] 5.3 Capture the verification output in `openspec/changes/add-upstream-sync-cron/notes.md`

## 6. Rollout

- [ ] 6.1 Run `/opsx:verify add-upstream-sync-cron` and address any findings
- [ ] 6.2 Run `/opsx:sync add-upstream-sync-cron` so the delta is reflected into `openspec/specs/upstream-sync/spec.md`
- [ ] 6.3 Run `/opsx:archive add-upstream-sync-cron`
- [ ] 6.4 Operator runs `scripts/setup_upstream_remote.sh` once
- [ ] 6.5 Operator (optionally) runs `scripts/install_launchd.sh` to enable scheduled sync
- [ ] 6.6 Operator monitors first two weeks of `sync-report` issues daily; if clean, relaxes to weekly review