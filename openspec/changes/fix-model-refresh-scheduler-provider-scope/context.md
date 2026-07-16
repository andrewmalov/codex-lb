# Context — fix-model-refresh-scheduler-provider-scope

## Live trace (claude-test.bezproblem.vip, 2026-07-15)

After PR #27 successfully added the Claude account via OAuth-link:

```
13:56:18Z claude.oauth.flow.callback   ← account row inserted, status=active
13:57:14Z WARNING  app.core.openai.model_refresh_scheduler
  Model fetch auth retry failed
  account=claude-491c2857-30eb-49ce-ad07-2b601efa041d plan=claude_subscription
  initial_error=status=401 transport=False
    message=HTTP 401: {"detail":"Could not parse your authentication token. Please try signing in again."}
  retry_error=code=token_expired permanent=True transport=False
    message=Could not parse your authentication token...
```

Within ~56 seconds the scheduler's tick fired, fetched `/codex/models`
with the Claude bearer, got 401, escalated to `mark_permanent_failure`,
flipped the row to `status='reauth_required'`, and the operator saw the
"Re-auth required" badge. This happens reliably for every Claude account
on every scheduler tick — `CODEX_LB_*_MODEL_REFRESH_INTERVAL_SECONDS`
default is 60s.

DB confirmation:

```
id:                       claude-491c2857-30eb-49ce-ad07-2b601efa041d
claude_user_email:         kusanat5@gmail.com
status:                   reauth_required
deactivation_reason:      Authentication token expired - re-login required
```

## Why provider separation matters

`fetch_models_for_plan` is hard-coded for the Codex upstream:
- URL: `{upstream_base_url}/codex/models?client_version=...`
- Headers: `Authorization: Bearer ...` AND `chatgpt-account-id: <id>`

Anthropic does not accept `chatgpt-account-id` (the request fails
validation upstream of the auth check, returning 401 with the generic
"Could not parse your authentication token"). The 401 looks identical
to a real expired-token failure, so the scheduler's
`mark_permanent_failure(state, "token_expired")` path runs and the Claude
account is correctly, but wrongly, marked reauth-required.

The scheduler iterates `accounts_repo.list_accounts()` which returns both
provider types — see `app/modules/accounts/repository.py:64`:

```python
async def list_accounts(self, *, refresh_existing: bool = False) -> list[Account]:
    stmt = select(Account).order_by(Account.email)
    if refresh_existing:
        stmt = stmt.execution_options(populate_existing=True)
    result = await self._session.execute(stmt)
    return list(result.scalars().all())
```

No `WHERE provider == ...` filter. So the scheduler passes Claude accounts
into a Codex-only fetcher.

## Why this is a separate change from PR #27

PR #27 fixed the OAuth-link account-add path (an upstream-only failure:
"Anthropic doesn't return id_token for the public client"). The
reauth-flip is downstream — the account row inserts correctly, but a
background scheduler corrupts it within ~60s. Different surface, different
test surface, different OpenSpec capability (`model-registry` vs
`claude-oauth-pool`).

## Non-claim about Codex behavior

The Codex fetcher path itself is unchanged by this change. We do not
redesign Codex auth or scheduling — only add provider filtering and a
Claude fetcher so the Claude rows don't get caught in the wrong path.

## Related

- `openspec/changes/diagnose-claude-oauth-add-blocker/` — original
  diagnosis from the 2026-07-15 incident.
- `openspec/changes/fix-claude-oauth-account-claims/` — PR #27 that fixed
  the OAuth-link account-add path. This change is its sequel.
- `openspec/specs/claude-oauth-pool/` — Claude OAuth pool capability
  spec. This change adds a runtime-integration requirement on top of
  it.