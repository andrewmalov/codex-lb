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

Note the absence of the ``account_id`` parameter: the Claude OAuth pool
has no Codex-style ``chatgpt_account_id`` and Anthropic rejects the
``chatgpt-account-id`` header. The scheduler's fetcher dispatch surfaces
this asymmetry via a ``fetcher_takes_account_id`` kwarg (see task 5).

Parse `{"data": [{"id": ..., "display_name": ...}, ...]}` to `UpstreamModel`.
On 401 → `ModelFetchError(status=401, ...)`; on timeout / transport
failure → `ModelFetchError(..., transport_error=True)`.

**Acceptance:** a stubbed transport returning Anthropic's `/v1/models`
JSON yields `UpstreamModel` entries; a 401 response surfaces as
`ModelFetchError(401, ...)`.

## 3. Wire Claude fetcher AND Claude auth manager into scheduler

**File:** `app/core/openai/model_refresh_scheduler.py`

After the Codex fetch block, add a parallel block for Claude accounts:

```python
claude_accounts = await accounts_repo.list_accounts_by_provider("claude")
if claude_accounts:
    claude_result = await _fetch_with_failover(
        candidates=claude_accounts,
        encryptor=encryptor,
        accounts_repo=accounts_repo,
        fetcher=fetch_claude_models,                         # new kwarg
        auth_manager_factory=claude_auth_manager_factory,    # provider-scoped
    )
    if claude_result is not None:
        per_plan_results.update(claude_result.models)        # plan_type == "claude_subscription"
        per_account_results.update(claude_result.account_models)
```

`_fetch_with_failover` gains two parameters:

- `fetcher: Callable` (default `fetch_models_for_plan`) — same failover
  loop serves both providers.
- `auth_manager_factory: Callable[[AccountsRepository], _AuthManagerLike]`
  (default `_default_auth_manager_factory`) — picks the Codex
  ``AuthManager`` for Codex accounts and a Claude-aware
  ``_ClaudeAuthManagerAdapter`` for Claude accounts.

The Codex auth manager's ``refresh_account`` reads
``refresh_token_encrypted``, which for Claude rows is the
``encrypt("claude")`` placeholder. The Claude adapter is a thin
Protocol shim that satisfies
``ensure_fresh(account, *, force=False) -> Account`` without invoking
Codex logic; Claude OAuth rotation is owned by the dedicated
``app.core.auth.guardian.AuthGuardianScheduler`` pass which writes the
rotated tokens back to the database ahead of the model-discovery tick.

**Acceptance:** the scheduler's `registry.update(...)` call receives
entries under `claude_subscription` after a successful Claude fetch; the
existing Codex fetch path is byte-for-byte unchanged; the Codex auth
manager is never instantiated against a Claude row.

## 4. Tests

**Files:**
- `tests/unit/test_model_refresh_scheduler.py` (existing) — add:
  - `test_refresh_filters_codex_accounts_from_claude_fetch`: build two
    Account rows (one Codex, one Claude), mock both transports, assert
    Codex path called once, Claude path called once.
  - `test_refresh_filters_claude_accounts_from_codex_fetch`: mirror of
    above.
  - `test_account_access_token_picks_claude_column_for_claude_rows`:
    direct unit test for the new ``_account_access_token`` resolver;
    the original ticket's blocker-A — a regression that decrypts
    ``account.access_token_encrypted`` for a Claude row would surface
    as ``encryptor.decrypt("claude")`` here.
  - `test_fetch_with_failover_uses_claude_auth_manager_for_claude_accounts`:
    pins the provider-scoped auth-manager wiring against the Codex
    constructor (blocker-B regression).
- `tests/unit/test_model_fetcher.py` (existing) — already covers
  `fetch_claude_models` from the previous round; the existing tests are
  updated for the new kwarg-only signature:
  - Happy path: Anthropic-style body → `UpstreamModel` list.
  - 401 → `ModelFetchError(401, ...)`.
  - 5xx → `ModelFetchError(502, ...)`.
  - Transport error → `ModelFetchError(..., transport_error=True)`.

**Acceptance:** all new tests pass; existing scheduler tests
(`tests/unit/test_model_refresh_scheduler.py`) still pass with the
**Codex path byte-identical** — the only behavior changes are
(a) ``_claude_account``/``_account`` fixtures encrypt their token columns
with the real ``TokenEncryptor`` so the resolver test is meaningful,
and (b) ``_invoke_fetcher`` plumbs a ``fetcher_takes_account_id`` kwarg.

## 5. Spec delta

**File:** `openspec/changes/fix-model-refresh-scheduler-provider-scope/specs/model-registry/spec.md`

Add the following ``ADDED Requirements``:

- **Model registry refresh — provider scope** (already shipped): the
  scheduler MUST iterate accounts partitioned by ``provider`` and route
  each partition to its provider-specific model catalog endpoint.
- **`fetch_claude_models` function signature**: drop the unused
  ``account_id`` positional arg; the function signature is now
  ``(access_token, *, route=..., codex_client=...,
  allow_direct_egress=...)``.
- **Provider-scoped bearer resolution**: a new
  ``_account_access_token`` resolver MUST decrypt
  ``Account.claude_access_token_encrypted`` for Claude rows and
  ``Account.access_token_encrypted`` for Codex rows.
- **Provider-scoped auth-manager selection**: ``_fetch_with_failover``
  MUST accept an ``auth_manager_factory``; the Codex branch wires the
  existing ``AuthManager`` factory, the Claude branch wires
  ``_ClaudeAuthManagerAdapter``. Constructing ``AuthManager`` against a
  Claude row is disallowed.
- **Provider-scoped fetcher dispatch**: ``_invoke_fetcher`` MUST call
  each fetcher with the right positional shape per provider — Codex
  receives ``(access_token, chatgpt_account_id)``, Claude receives
  ``(access_token,)``.

**Acceptance:** `openspec validate --strict` clean.

## 6. Verify on test bench

After PR merge and auto-deploy:
- Add a fresh Claude account via OAuth-link.
- Wait > scheduler interval (`CODEX_LB_*` default 60s).
- Confirm the account remains `status='active'`.
- Confirm `claude-491c2857-...` (the prior victim of the bug) flipped
  back to `active` after the re-auth.