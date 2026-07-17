# 2026-07-17 incident: Claude account flips to `reauth_required` within 5 s of OAuth callback

## Server

`claude-test.bezproblem.vip` (Linux, Docker, single replica).
Image: `ghcr.io/andrewmalov/codex-lb:main` — built from `e3e69afe`
("fix(model-refresh): route bearer and auth manager per provider (#30)").

## Timeline (UTC, 2026-07-17)

```
09:26:17  container codex-lb-server-1 created (image built from main after PR #30 merge)
09:58:11  POST /api/claude/oauth/start    ← operator starts OAuth flow
09:59:25  WARNING  claude.oauth.flow.callback.diagnostic
09:59:25  INFO     claude.oauth.flow.callback       account_id=claude-491c2857-30eb-49ce-ad07-2b601efa041d status=success
09:59:25  INFO     POST /api/claude/oauth/callback   200 OK
09:59:25  INFO     GET  /api/claude/accounts         200 OK     (×2)
09:59:29  INFO     GET  /api/accounts/claude-491c.../trends              200 OK
09:59:29  WARNING  dashboard_error_response ... path=/api/accounts/claude-491c.../usage-reset-credits status=409
09:59:29  INFO     GET  /api/accounts/claude-491c.../usage-reset-credits  409 Conflict
09:59:29  WARNING  app.core.clients.usage     Usage fetch failed  request_id=None status=401 code=None message=Usage fetch failed (401)
09:59:29  WARNING  app.core.auth.refresh      Token refresh failed request_id=None status=401
09:59:30  INFO     GET  /api/accounts/claude-491c.../usage-reset-credits  409 Conflict   ← REAUTH_REQUIRED already in DB
10:01:28  WARNING  app.core.openai.model_refresh_scheduler Model registry refresh produced no results despite candidates providers_with_candidates=1
10:06:28  WARNING  ...providers_with_candidates=1     ← every model-refresh tick for the next hour
10:11:28  WARNING  ...providers_with_candidates=1
```

The operator's observation was identical to the 2026-07-15 incident:
"Claude account fell into re-auth required". The dashboard could no
longer fetch usage reset credits for the account.

## Root cause

`AuthManager.refresh_account` (`app/modules/accounts/auth_manager.py:206-226`)
reads `account.refresh_token_encrypted` and decrypts it. For Claude rows
the column holds the literal placeholder `"claude"` (encrypted) — set
by `ClaudeAuthManager.add_claude_account:279-281` because the
`ck_accounts_claude_rt_required` check constraint only requires
`claude_refresh_token_encrypted`:

```python
"access_token_encrypted": self._encryptor.encrypt("claude"),
"refresh_token_encrypted": self._encryptor.encrypt("claude"),
"id_token_encrypted": self._encryptor.encrypt("claude"),
```

`refresh_account` then POSTs `"claude"` as `refresh_token` to the Codex
OAuth endpoint (`{auth_base_url}/oauth/token`). The endpoint returns
`400 {"error": "invalid_grant"}` and `_refresh_tokens` raises
`RefreshError(code="invalid_grant", is_permanent=True)`. The except
branch at `auth_manager.py:218-225` writes the permanent-failure status:

```python
if exc.is_permanent:
    ...
    status = account_status_for_permanent_failure(exc.code)  # REAUTH_REQUIRED
    await self._repo.update_status(account.id, status, reason)
    account.status = status
```

The Claude account is therefore flipped to `reauth_required` within
one tick of the dashboard polling `/usage-reset-credits` — typically
~5 seconds after the OAuth callback.

## Call sites of `AuthManager.ensure_fresh` for Claude rows

The proxy pool excludes Claude rows via `load_balancer.py:786-824`
(`effective_provider = "codex" if provider is None else provider`).
The proxy path is therefore NOT a surface for this bug. The surfaces
that DO touch `AuthManager.ensure_fresh` for Claude rows are:

- `app/modules/accounts/service.py:240` — `get_usage_reset_credits`
  (dashboard)
- `app/modules/accounts/service.py:272` — `get_usage_reset_credits`
  forced-retry on 401
- `app/modules/accounts/service.py:303` — `consume_usage_reset_credit`
  (dashboard)
- `app/modules/accounts/service.py:366` — `consume_usage_reset_credit`
  forced-retry on 401
- `app/modules/accounts/service.py:702` — account probe
- `app/modules/usage/updater.py:418` — usage refresh scheduler
  forced-retry

The dashboard `/usage-reset-credits` is the first one that fires after
OAuth callback (the dashboard polls it immediately), so it is the
surface that surfaced the bug.

## Why PR #30 didn't catch this

PR #30 introduced `_ClaudeAuthManagerAdapter` in
`app/core/openai/model_refresh_scheduler.py` — a no-op stand-in for
the Codex `AuthManager` that the scheduler's `_fetch_with_failover`
loop instantiates per provider. The scheduler was the only place that
was provider-aware at the `AuthManager` boundary. The rest of the
codebase still instantiates `AuthManager(accounts_repo)` directly, with
no provider check. The fix must extend the same provider-scoped
deferral to `AuthManager.refresh_account` itself so the short-circuit
applies no matter which caller invokes it.

## Architectural note

The Codex `AuthManager` is the wrong tool for Claude OAuth because:

1. It decrypts the Codex-flavored `refresh_token_encrypted` column —
   which holds the placeholder `"claude"` for Claude rows.
2. It POSTs to the Codex OAuth endpoint — Claude has its own token
   endpoint (`https://platform.claude.com/v1/oauth/token`), client_id
   (`9d1c250a-...`), and scope.
3. It writes the new access token back to `access_token_encrypted` —
   overwriting the placeholder would set the Claude account's Codex
   column to a fresh Codex token, which is wrong on multiple axes.

Claude rotation is therefore owned by `ClaudeAuthManager.rotate_claude_access_token`
(`app/modules/claude/auth_manager.py:359`), invoked from
`app.core.auth.guardian.AuthGuardianScheduler`. The Codex AuthManager
must NOT be in the rotation path for Claude rows.

## Verification matrix (post-fix)

| Call site                                                | Pre-fix                            | Post-fix |
|----------------------------------------------------------|------------------------------------|----------|
| `service.get_usage_reset_credits` (dashboard)            | `update_status(REAUTH_REQUIRED)`   | no-op    |
| `service.consume_usage_reset_credit` (dashboard)         | `update_status(REAUTH_REQUIRED)`   | no-op    |
| `service.account probe`                                  | `update_status(REAUTH_REQUIRED)`   | no-op    |
| `usage.updater` force retry                              | `update_status(REAUTH_REQUIRED)`   | no-op    |
| `proxy.service` ensure_fresh                             | not invoked (Claude excluded)      | unchanged|
| `proxy._service.warmup`                                  | not invoked (Claude excluded)      | unchanged|
| `proxy._service.streaming/websocket` mixin               | not invoked (Claude excluded)      | unchanged|
| `model_refresh_scheduler` _fetch_with_failover           | handled by `_ClaudeAuthManagerAdapter` | unchanged |

All four rows that flip status pre-fix are still called for Claude
rows (the dashboard polls them on every page-load and the usage
refresh scheduler ticks every 60 s); they must become no-ops for
`provider='claude'`.