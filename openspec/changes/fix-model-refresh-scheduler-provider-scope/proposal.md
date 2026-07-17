# fix-model-refresh-scheduler-provider-scope

## Why

`app.core.openai.model_refresh_scheduler.ModelRefreshScheduler._refresh_once`
iterates over `accounts_repo.list_accounts()` — which returns BOTH Codex and
Claude provider rows — and feeds every account's token into
`fetch_models_for_plan(access_token, account_id, ...)`. That fetcher builds
a URL against `settings.upstream_base_url/codex/models` and sets the
`chatgpt-account-id` header. The Codex upstream returns
`HTTP 401 "Could not parse your authentication token. Please try signing in
again."` when the bearer is a Claude OAuth access token (issued by the
public client `9d1c250a-...`, audience Anthropic), and the scheduler then
escalates to `mark_permanent_failure(state, "token_expired")`, flipping the
Claude account to `status='reauth_required'` within ~60 seconds of being
added (or any time the refresh tick fires).

Live trace captured on `claude-test.bezproblem.vip` 2026-07-15 (after
PR #27 successfully added the Claude account via OAuth-link):

```
13:56:18 claude.oauth.flow.callback   ← account added, status=active
13:57:14 Model fetch auth retry failed
  account=claude-491c2857-30eb-49ce-ad07-2b601efa041d plan=claude_subscription
  initial_error=HTTP 401: {"detail":"Could not parse your authentication token..."}
  retry_error=code=token_expired permanent=True
```

This makes every Claude account unusable in production: the refresh
scheduler runs every `interval_seconds` (60s default), so the flip-to-reauth
happens within one minute of account creation. Operators would observe
their freshly-added Claude account become "Re-auth required" without ever
making a real Claude API call.

## What changes

### 1. Provider-scoped account iteration

`ModelRefreshScheduler._refresh_once` filters `list_accounts()` to
`provider='codex'` for the existing Codex-upstream fetch path. Claude
accounts are excluded from the Codex fetch.

### 2. New `fetch_claude_models` against Anthropic's model catalog

A new function `fetch_claude_models(access_token, *, route=None,
codex_client=None, allow_direct_egress=False)` in
`app.core.clients.model_fetcher` calls
`GET {claude_api_base_url}/v1/models` with `Authorization: Bearer ...` and
no `chatgpt-account-id` header (Anthropic doesn't accept it). The
response shape (`{"data": [{"id": "...", "display_name": "...", ...}]}`)
parses to `list[UpstreamModel]` and is grouped under plan
`claude_subscription`.

The signature drops the unused `account_id` parameter from the
original PR — the Claude OAuth pool has no Codex-style
`chatgpt_account_id` to forward, and the dispatcher surfaces this
asymmetry via a `fetcher_takes_account_id` kwarg on
`_invoke_fetcher` so the Claude branch receives only the access token
positionally.

### 3. Scheduler wires Claude fetcher in

After the Codex fetch, the scheduler iterates Claude accounts (also via
`list_accounts_by_provider('claude')`) and runs the same failover loop
with `fetch_claude_models`. Failure handling mirrors the existing Codex
path: 401 → refresh-token rotation retry → `token_expired` permanent
marks the account `reauth_required`.

### 4. Provider-scoped bearer resolution

A new helper `_account_access_token(encryptor, account)` decrypts the
correct encrypted column per provider:

- `Account.provider == 'claude'` → decrypt
  `account.claude_access_token_encrypted` (the real Claude bearer).
- `Account.provider == 'codex'` → decrypt `account.access_token_encrypted`
  (the Codex bearer).

This fixes the silent regression where the scheduler unconditionally
read `account.access_token_encrypted` for every account — for Claude
accounts that column holds `encrypt("claude")` (the placeholder the
NOT-NULL constraint forced) and decrypts to the literal string
`"claude"`. The Codex upstream's response to `Bearer claude` was a
permanent 401; the scheduler then marked the account `reauth_required`
inside one tick.

### 5. Provider-scoped auth-manager selection

The existing `AuthManager(accounts_repo)` constructor returns the Codex
auth manager whose `refresh_account` reads `refresh_token_encrypted`
(the same placeholder column for Claude rows). `_fetch_with_failover`
now accepts an `auth_manager_factory: Callable[[AccountsRepository],
_AuthManagerLike] | None` kwarg; the default wires `AuthManager(repo)`
for the Codex branch, and `_refresh_once` passes a
`_ClaudeAuthManagerAdapter` factory for the Claude branch.

The Claude adapter is a thin Protocol shim satisfying
`ensure_fresh(account, *, force=False) -> Account`. It is a no-op for
the rotation pass because Claude OAuth rotation is owned by the
dedicated `app.core.auth.guardian.AuthGuardianScheduler` pass which
writes the rotated tokens back to the database ahead of the
model-discovery tick. The adapter's only job is to keep the failover
loop from instantiating the Codex auth manager against Claude rows.

### Non-goals

- **No new `plan_type` for Claude**. `claude_subscription` already exists
  in the registry; this change routes model results to it.
- **No switch away from `api.anthropic.com`.** Operators override
  `CODEX_LB_CLAUDE_API_BASE_URL` already; `fetch_claude_models` honors it.
- **No retry-budget rework**. The Claude fetcher reuses the same
  `RefreshError` path the existing Codex path uses; nothing new in the
  refresh scheduler's lock/state machine.
- **No column-layout change**. The placeholder semantics in
  `app/modules/claude/auth_manager.py` stay; the fix is purely a
  resolver that picks the right column.

## Success criteria

- Adding a fresh Claude OAuth account via the dashboard does NOT cause
  `model_refresh_scheduler` to flip it to `reauth_required` within the
  scheduler interval.
- `registry.get_snapshot()` lists Claude models under plan
  `claude_subscription` after a successful refresh.
- Existing Codex model refresh behavior is unchanged
  (byte-identical `AuthManager(repo)` path for Codex rows).
- The fix is covered by tests: scheduler filters by provider, Claude
  fetcher hits the right endpoint, the bearer resolver picks the right
  column per account, the auth-manager factory routes Claude rows
  through the Claude adapter, and failure modes match existing Codex
  handling.