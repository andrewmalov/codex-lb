# model-registry Specification (delta)

## ADDED Requirements

### Requirement: Model registry refresh — provider scope

The model refresh scheduler MUST iterate accounts partitioned by `provider`
and route each partition to its provider-specific model catalog endpoint.
Specifically:

- **Codex-provider accounts** (`Account.provider == 'codex'`) — refresh via
  the existing Codex upstream endpoint
  `{upstream_base_url}/codex/models?client_version=...`. The request
  carries the account's bearer token AND a `chatgpt-account-id` header.
- **Claude-provider accounts** (`Account.provider == 'claude'`) — refresh
  via the Anthropic endpoint `{claude_api_base_url}/v1/models`. The
  request carries the account's bearer token. The `chatgpt-account-id`
  header MUST NOT be sent (Anthropic rejects unknown headers).

A bearer token issued for one provider MUST NOT be sent to the other's
upstream. When this happens (e.g., a regression that re-introduces the
cross-provider leak), the receiving upstream returns an authentication
error that the scheduler MUST surface as either a transient refresh
failure (with the existing retry path) or a permanent failure that marks
the account `status='reauth_required'` — i.e., the same handling that
already exists for legitimate provider-specific token failures.

#### Scenario: Fresh Claude account survives the scheduler tick

- **GIVEN** a Codex account (active) and a Claude account (active, just
  added via OAuth-link)
- **WHEN** the model refresh scheduler fires (`_refresh_once`)
- **THEN** the Codex account's token is sent to `{upstream_base_url}/codex/models`
  exactly once
- **AND** the Claude account's token is sent to `{claude_api_base_url}/v1/models`
  exactly once
- **AND** the Claude account's `status` remains `active` after the tick
  (no false `reauth_required` flip).

#### Scenario: Cross-provider bearer leak is contained

- **GIVEN** a regression that sends a Claude account's bearer to
  `{upstream_base_url}/codex/models`
- **WHEN** the upstream returns `HTTP 401`
- **THEN** the scheduler's existing retry-with-rotation path runs
- **AND** if rotation also fails with `code=token_expired permanent=True`,
  the Claude account is marked `reauth_required` (matching the existing
  permanent-failure contract).

#### Scenario: Codex model registry unchanged for operators

- **GIVEN** only Codex accounts in the pool (no Claude)
- **WHEN** the scheduler runs
- **THEN** the Codex fetch path runs unchanged
- **AND** no Claude-related calls are made (no surprise network traffic).

### Requirement: `fetch_claude_models` function signature

`app.core.clients.model_fetcher.fetch_claude_models` SHALL be exposed with
this contract:

```
async def fetch_claude_models(
    access_token: str,
    *,
    route: ResolvedUpstreamRoute | None = None,
    codex_client: CodexClient | None = None,
    allow_direct_egress: bool = False,
) -> list[UpstreamModel]
```

The function MUST:
- Build the URL as `{claude_api_base_url.rstrip("/")}/v1/models`.
- Send `Authorization: Bearer {access_token}` only (no
  `chatgpt-account-id`).
- Send `anthropic-version: "2023-06-01"` and `Accept: application/json`.
- Parse `{"data": [{"id": str, "display_name": str | None, ...}]}` into
  `UpstreamModel` instances.
- Raise `ModelFetchError(status, message)` for 4xx/5xx.
- Raise `ModelFetchError(504, ..., transport_error=True)` on timeout;
  `ModelFetchError(0, ..., transport_error=True)` on connection
  errors.

This contract deliberately does NOT accept an ``account_id`` parameter:
the Claude OAuth pool has no Codex-style ``chatgpt_account_id`` to
forward, and Anthropic rejects the ``chatgpt-account-id`` header
outright. The model refresh scheduler's fetcher dispatch surfaces this
asymmetry explicitly (see *Provider-scoped fetcher dispatch* below).

#### Scenario: Anthropic models endpoint happy path

- **GIVEN** a stubbed HTTP transport returning
  ```json
  {
    "data": [
      {"id": "claude-opus-4-20250514", "display_name": "Claude Opus 4"},
      {"id": "claude-sonnet-4-20250514", "display_name": "Claude Sonnet 4"}
    ]
  }
  ```
- **WHEN** `fetch_claude_models("sk-ant-oat01-...")` runs
- **THEN** it returns two `UpstreamModel` entries with `model_id="claude-opus-4-..."`
  and `model_id="claude-sonnet-4-..."`.
- **AND** the outgoing request does NOT contain a `chatgpt-account-id`
  header.

#### Scenario: 401 surfaces as `ModelFetchError`

- **GIVEN** a stubbed HTTP transport returning `401` with body
  `{"detail": "Could not parse your authentication token..."}`
- **WHEN** `fetch_claude_models(...)` runs
- **THEN** it raises `ModelFetchError(401, ...)` with `transport_error=False`.

#### Scenario: Transport timeout surfaces as `ModelFetchError(transport_error=True)`

- **GIVEN** a stubbed HTTP transport that times out after `_FETCH_TIMEOUT_SECONDS`
- **WHEN** `fetch_claude_models(...)` runs
- **THEN** it raises `ModelFetchError(504, ..., transport_error=True)`.

### Requirement: Provider-scoped bearer resolution

The model refresh scheduler MUST decrypt access tokens using a
provider-aware resolver that picks the correct encrypted column for the
account:

- **Codex accounts** (`Account.provider == 'codex'`) — decrypt
  `Account.access_token_encrypted`.
- **Claude accounts** (`Account.provider == 'claude'`) — decrypt
  `Account.claude_access_token_encrypted`.

The provider-agnostic ``access_token_encrypted`` column is populated by
``app/modules/claude/auth_manager.py:add_claude_account`` with
``encrypt("claude")`` as a placeholder (the column is ``NOT NULL`` at the
table level — see the Alembic migration in
``app/db/alembic/versions/20260701_000000_add_claude_account_columns.py``).
Decrypting that placeholder returns the literal string ``"claude"``;
sending ``Bearer claude`` to any upstream is treated as a permanent
authentication failure by both Anthropic and OpenAI. A regression that
reads the wrong column for a Claude account therefore surfaces as a
spurious ``reauth_required`` flip within one scheduler tick.

#### Scenario: Claude bearer is decrypted from the Claude column

- **GIVEN** a Claude account whose ``claude_access_token_encrypted``
  decrypts to ``"sk-ant-real-bearer"`` and whose
  ``access_token_encrypted`` decrypts to ``"claude"`` (the placeholder)
- **WHEN** the provider-aware resolver runs against the account
- **THEN** it returns ``"sk-ant-real-bearer"``
- **AND** the scheduler forwards that exact value to
  ``fetch_claude_models`` (verifiable by inspecting the dispatcher
  ``await_args.args[0]``)
- **AND** the placeholder string is never sent to any upstream.

#### Scenario: Codex bearer is decrypted from the Codex column

- **GIVEN** a Codex account whose ``access_token_encrypted`` decrypts to
  a real bearer token
- **WHEN** the resolver runs against the account
- **THEN** it returns that bearer unchanged
- **AND** ``claude_access_token_encrypted`` (NULL for Codex rows) is
  NOT consulted.

### Requirement: Provider-scoped auth-manager selection

The model refresh scheduler MUST select a provider-aware auth manager per
provider partition when calling ``ensure_fresh(account, *, force=False)``
ahead of each model fetch:

- **Codex accounts** — the scheduler MUST instantiate
  ``app.modules.accounts.auth_manager.AuthManager`` (the Codex auth
  manager). Its ``refresh_account`` reads ``refresh_token_encrypted``.
- **Claude accounts** — the scheduler MUST NOT instantiate
  ``AuthManager``; it MUST route through a Claude-aware adapter. The
  Codex manager against a Claude row would either silently swallow a
  placeholder refresh-token decrypt or surface a nonsensical refresh
  failure, both of which defeat the scheduler's existing failure
  handling.

The Claude-aware adapter is a thin Protocol shim satisfying the same
``ensure_fresh(account, *, force=False) -> Account`` contract that the
Codex manager exposes. The dedicated auth guardian
(``app.core.auth.guardian.AuthGuardianScheduler``) owns Claude OAuth
rotation in production, so the model refresh scheduler's Claude adapter
is a no-op for the rotation pass. The model's job is to (a) be a
Protocol-conforming replacement that does not invoke Codex code paths
against Claude rows, and (b) continue running the discovery loop if a
Claude access token is mildly stale — the guardian catches up on its
next tick.

#### Scenario: Claude branch constructs a Claude-side auth manager

- **GIVEN** a Claude account row added via the OAuth-link flow
- **WHEN** the model refresh scheduler fires ``_refresh_once``
- **THEN** the failover loop iterates Claude accounts with a
  Claude-aware auth manager factory
- **AND** ``AuthManager`` is NOT constructed against Claude accounts.

#### Scenario: Codex branch constructs the Codex auth manager

- **GIVEN** only Codex accounts in the pool
- **WHEN** the model refresh scheduler fires ``_refresh_once``
- **THEN** the existing ``AuthManager(accounts_repo)`` path runs for
  every Codex account — the byte-for-byte production behavior.
- **AND** no Claude-related calls are made.

### Requirement: Provider-scoped fetcher dispatch

The model refresh scheduler's fetcher dispatch MUST call each fetcher
with the right positional shape:

- **Codex fetcher** (``fetch_models_for_plan``) — receive
  ``(access_token, account_id, *, route=..., allow_direct_egress=...)``
  so the existing ``chatgpt_account_id`` header behavior is preserved.
- **Claude fetcher** (``fetch_claude_models``) — receive
  ``(access_token, *, route=..., allow_direct_egress=...)``. There is no
  ``account_id`` to forward and the header would be rejected upstream.

The shape is plumbed through ``_invoke_fetcher``'s
``fetcher_takes_account_id`` keyword so tests may substitute any
callable for either fetcher.

#### Scenario: Anthropic fetcher receives only an access token

- **GIVEN** the scheduler invokes the dispatcher with the Claude
  fetcher and a Claude account
- **WHEN** the dispatcher reaches the Claude branch
- **THEN** the fetcher is called with exactly one positional arg
  (``access_token``)
- **AND** ``route`` and ``allow_direct_egress`` are passed as keyword
  arguments.

#### Scenario: Codex fetcher receives access_token and account_id

- **GIVEN** the scheduler invokes the dispatcher with the Codex fetcher
  and a Codex account whose ``chatgpt_account_id`` is set
- **WHEN** the dispatcher reaches the Codex branch
- **THEN** the fetcher is called with positional ``(access_token,
  chatgpt_account_id)``
- **AND** ``route`` and ``allow_direct_egress`` are passed as keyword
  arguments.