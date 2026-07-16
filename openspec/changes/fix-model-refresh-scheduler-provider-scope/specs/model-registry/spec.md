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
    account_id: str | None,
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

This contract mirrors `fetch_models_for_plan` so the existing
`_fetch_with_failover` loop can be parameterized by fetcher.

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
- **WHEN** `fetch_claude_models("sk-ant-oat01-...", account_id=None)` runs
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