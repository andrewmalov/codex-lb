# Tasks

## 1. Filter Codex fetch by provider

**File:** `app/core/openai/model_refresh_scheduler.py`

In `_refresh_once` (currently `accounts = await accounts_repo.list_accounts()`
at line 89), restrict the loop input for the Codex fetch to
`provider='codex'`. Two options:

- Add a one-off `await accounts_repo.list_accounts_by_provider("codex")`
  helper on `AccountsRepository` (preferred — symmetric with future
  `list_accounts_by_provider("claude")`).
- Or filter inline via `WHERE Account.provider == 'codex'` in the
  scheduler loop.

The grouped fetch results stay keyed by plan_type as today; the
`_group_by_plan` helper takes the filtered list.

**Acceptance:** an environment with one Codex account (active) and one
Claude account (active) results in `_refresh_once` making exactly one
Codex-upstream fetch (the Codex account's bearer), zero Claude-account
fetches against the Codex upstream.

## 2. Add `fetch_claude_models`

**File:** `app/core/clients/model_fetcher.py`

New function:

```python
async def fetch_claude_models(
    access_token: str,
    account_id: str | None,
    *,
    route: ResolvedUpstreamRoute | None = None,
    codex_client: CodexClient | None = None,
    allow_direct_egress: bool = False,
) -> list[UpstreamModel]:
    settings = get_settings()
    base = settings.claude_api_base_url.rstrip("/")
    url = f"{base}/v1/models"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "anthropic-version": "2023-06-01",
        "Accept": "application/json",
    }
    # No chatgpt-account-id header — Anthropic rejects it.
    # ... rest mirrors fetch_models_for_plan with ModelFetchError shape.
```

Parse `{"data": [{"id": ..., "display_name": ...}, ...]}` to `UpstreamModel`.
On 401 → `ModelFetchError(status=401, ...)`; on timeout / transport
failure → `ModelFetchError(..., transport_error=True)`.

**Acceptance:** a stubbed transport returning Anthropic's `/v1/models`
JSON yields `UpstreamModel` entries; a 401 response surfaces as
`ModelFetchError(401, ...)`.

## 3. Wire Claude fetcher into scheduler

**File:** `app/core/openai/model_refresh_scheduler.py`

After the Codex fetch block, add a parallel block for Claude accounts:

```python
claude_accounts = await accounts_repo.list_accounts_by_provider("claude")
if claude_accounts:
    claude_result = await _fetch_with_failover(
        candidates=claude_accounts,
        encryptor=encryptor,
        accounts_repo=accounts_repo,
        fetcher=fetch_claude_models,   # new kwarg on _fetch_with_failover
    )
    if claude_result is not None:
        per_plan_results.update(claude_result.models)        # plan_type == "claude_subscription"
        per_account_results.update(claude_result.account_models)
```

`_fetch_with_failover` gains a `fetcher: Callable` parameter (default
`fetch_models_for_plan`) so the same failover loop serves both providers.
The 401 → refresh-retry path stays identical — Claude's refresh path is
already implemented in `ClaudeAuthManager._run_refresh`; the scheduler
already calls `auth_manager` correctly because the Account row carries a
`provider` discriminator and the right auth manager is selected upstream
in `proxy/load_balancer.py` and `claude/auth_manager.py`.

**Acceptance:** the scheduler's `registry.update(...)` call receives
entries under `claude_subscription` after a successful Claude fetch; the
existing Codex fetch path is byte-for-byte unchanged.

## 4. Tests

**Files:**
- `tests/unit/test_model_refresh_scheduler.py` (existing) — add:
  - `test_refresh_filters_codex_accounts_from_claude_fetch`: build two
    Account rows (one Codex, one Claude), mock both transports, assert
    Codex path called once, Claude path called once.
  - `test_refresh_filters_claude_accounts_from_codex_fetch`: mirror of
    above.
- `tests/unit/test_model_fetcher.py` (new) — cover
  `fetch_claude_models`:
  - Happy path: Anthropic-style body → `UpstreamModel` list.
  - 401 → `ModelFetchError(401, ...)`.
  - 5xx → `ModelFetchError(502, ...)`.
  - Transport error → `ModelFetchError(..., transport_error=True)`.

**Acceptance:** all new tests pass; existing scheduler tests
(`tests/unit/test_model_refresh_scheduler.py`) still pass byte-for-byte
(no signature change beyond `fetcher=` kwarg on `_fetch_with_failover`,
which has a default).

## 5. Spec delta

**File:** `openspec/changes/fix-model-refresh-scheduler-provider-scope/specs/model-registry/spec.md`

Add one `ADDED Requirement`:

> **Model registry refresh — provider scope**
> The model refresh scheduler MUST iterate accounts partitioned by
> ``provider``. Codex-provider accounts are refreshed via the existing
> Codex upstream ``/codex/models`` endpoint; Claude-provider accounts
> are refreshed via the Anthropic ``{claude_api_base_url}/v1/models``
> endpoint. A bearer token issued for one provider MUST NOT be sent to the
> other's upstream; doing so is treated as a transient or permanent
> failure (mirroring existing handling).

**Acceptance:** `openspec validate --strict` clean.

## 6. Verify on test bench

After PR merge and auto-deploy:
- Add a fresh Claude account via OAuth-link.
- Wait > scheduler interval (`CODEX_LB_*` default 60s).
- Confirm the account remains `status='active'`.
- Confirm `claude-491c2857-...` (the prior victim of the bug) flipped
  back to `active` after the re-auth.