# fix-auth-manager-claude-provider-defer

## Why

PR #30 (`fix-model-refresh-scheduler-provider-scope`) closed the model
refresh scheduler's bearer/auth-manager routing for Claude accounts. It
introduced a no-op `_ClaudeAuthManagerAdapter` so the scheduler no longer
sends a Claude OAuth bearer to the Codex upstream and no longer
instantiates the Codex `AuthManager` for Claude rows. The account no
longer flips to `reauth_required` from the scheduler.

The same anti-pattern still exists in `AuthManager.refresh_account` —
the code path called from `app/modules/accounts/service.py`,
`app/modules/usage/updater.py`, and elsewhere. For a Claude row:

1. `refresh_account` reads `account.refresh_token_encrypted` and decrypts
   it. For Claude rows the column holds the literal placeholder
   `"claude"` (encrypted) because the table constraint
   ``ck_accounts_claude_rt_required`` only requires
   `claude_refresh_token_encrypted` (see
   `app/modules/claude/auth_manager.py:279-281`).
2. It POSTs that placeholder to the Codex OAuth endpoint
   (`{auth_base_url}/oauth/token`) with the Codex `client_id`.
3. The Codex endpoint returns `400 {"error": "invalid_grant"}`.
4. `RefreshError(is_permanent=True, code="invalid_grant")` propagates.
5. `AuthManager.refresh_account:218-225` calls
   `repo.update_status(account.id, REAUTH_REQUIRED, reason)` — the
   Claude account flips to `reauth_required` within one tick of the
   dashboard polling `/usage-reset-credits` or the usage-refresh
   scheduler running.

Live trace captured on `claude-test.bezproblem.vip` 2026-07-17 (after
PR #30 was deployed and a Claude account was re-added via OAuth-link):

```
09:59:25 claude.oauth.flow.callback            account_id=claude-491c2857-...  status=success
09:59:29 dashboard_error_response              path=/api/accounts/claude-491c2857-.../usage-reset-credits status=409
09:59:29 Usage fetch failed                    status=401
09:59:29 Token refresh failed                  status=401
10:01:28 Model registry refresh produced no results despite candidates providers_with_candidates=1
```

After the 09:59:29 401, the next dashboard poll of the account's
`/usage-reset-credits` keeps returning `409 Conflict`. The
`AccountUsageResetCreditsUnavailableError` is raised when
`account.status in (PAUSED, REAUTH_REQUIRED, DEACTIVATED)` — so the
account IS `reauth_required` by 09:59:30, ~5 s after OAuth success. The
proxy load balancer is provider-scoped (Claude rows are excluded from
the Codex pool via `load_balancer._selectable_accounts` / the
`effective_provider = "codex" if provider is None else provider` branch
in `app/modules/proxy/load_balancer.py:786-824`), so the bug surface is
not the proxy. It is the dashboard endpoints and the usage-refresh
scheduler that touch `AuthManager.ensure_fresh` / `refresh_account` for
Claude rows.

## What changes

### `AuthManager.refresh_account` short-circuits for `provider='claude'`

Mirror the rationale from PR #30's `_ClaudeAuthManagerAdapter`:
rotation of Claude OAuth credentials is owned by
`app.core.auth.guardian.AuthGuardianScheduler` (singleflight), so the
Codex `AuthManager` must not introduce a second rotation surface.
For Claude rows, `refresh_account` returns the account unchanged and
does NOT call `refresh_access_token`, `repo.update_status`,
`repo.update_tokens`, or `mark_account_routing_unavailable`.

### `AuthManager.ensure_fresh` honors the same short-circuit

`ensure_fresh` calls `_REFRESH_SINGLEFLIGHT.run` → `_run_refresh` →
`refresh_account`, so the short-circuit at `refresh_account` is enough.
The `_REFRESH_SINGLEFLIGHT` failure cache is keyed by the encrypted
refresh-token fingerprint; for Claude rows we want to short-circuit
before the singleflight (to avoid the singleflight's own state being
polluted with a stale placeholder fingerprint), so the check lives at
the top of `refresh_account` and `ensure_fresh` does not need to change.

## What is NOT changed

- Proxy pool selection (`load_balancer._selectable_accounts` and the
  `effective_provider` branch in `load_balancer.py:786-824`) already
  filters Claude rows out of the Codex pool. The bug does not surface
  there.
- `app/modules/proxy/_service/{warmup,streaming,websocket}/...` and
  `app/modules/limit_warmup/service.py` decrypt `access_token_encrypted`
  for warmup requests, but these paths are Codex-only and the load
  balancer does not route Claude rows into them.
- The existing `_ClaudeAuthManagerAdapter` in
  `app/core/openai/model_refresh_scheduler.py` stays — the scheduler
  explicitly avoids instantiating the Codex `AuthManager` for Claude
  rows and that boundary is unchanged.
- `ClaudeAuthManager.rotate_claude_access_token` in
  `app/modules/claude/auth_manager.py` (the rotation owned by the
  auth guardian) is unchanged.

## Spec deltas

- `model-registry/spec.md` — adds a Requirement: "Account refresh /
  rotation MUST be provider-scoped. The Codex `AuthManager` MUST NOT
  attempt to rotate credentials for `provider='claude'` rows; rotation
  for Claude rows is owned by `app.core.auth.guardian.AuthGuardianScheduler`."
- `claude-oauth-pool/spec.md` — adds a Requirement: "Dashboard endpoints
  and the usage-refresh scheduler MUST tolerate Claude rows without
  triggering a Codex-flavored refresh; transient unavailability of
  reset-credits / usage for Claude rows is rendered as
  `AccountUsageResetCreditsUnavailableError`, not as a status flip to
  `reauth_required`."

## Refs

- Predecessor: PR #30 (`fix-model-refresh-scheduler-provider-scope`) —
  closed the scheduler path; this change closes the dashboard / usage
  scheduler paths.
- `openspec/changes/diagnose-claude-oauth-add-blocker/` — original
  diagnosis.
- 2026-07-15 incident: scheduler flipped Claude account within 60 s of
  OAuth callback. Fixed by PR #30.
- 2026-07-17 incident: dashboard `/usage-reset-credits` flipped Claude
  account within ~5 s of OAuth callback. Fixed by this change.
