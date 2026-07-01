# claude-oauth-pool Specification (delta)

## ADDED Requirements

### Requirement: Manual Claude account add

The system SHALL expose `POST /api/claude/accounts` to add a Claude account. The body SHALL accept:
- `claudeAccountUuid` (required string)
- `accessToken` (required string)
- `refreshToken` (required string)
- `expiresInSeconds` (required positive integer)
- `scopes` (optional list of strings)
- `userEmail` (optional string)
- `userOrganizationUuid` (optional string)

The system SHALL encrypt `accessToken` and `refreshToken` with the existing `app/core/crypto.py` envelope before persistence, store `claude_access_token_expires_at = now() + expiresInSeconds - skew`, and reject duplicate `claudeAccountUuid` with HTTP 409.

#### Scenario: Add Claude account happy path

- **WHEN** admin submits `POST /api/claude/accounts` with valid `claudeAccountUuid` and tokens
- **THEN** the response returns the new account id
- **AND** tokens are stored encrypted (no plaintext in the row bytes)

#### Scenario: Reject duplicate claudeAccountUuid

- **GIVEN** a Claude account with `claudeAccountUuid='abc-123'` already exists
- **WHEN** admin submits `POST /api/claude/accounts` with the same `claudeAccountUuid`
- **THEN** the system returns 409

#### Scenario: Reject missing required fields

- **WHEN** admin submits `POST /api/claude/accounts` without `refreshToken`
- **THEN** the system returns 400

### Requirement: List Claude accounts

The system SHALL expose `GET /api/claude/accounts` returning a JSON array of accounts with at minimum: `id`, `claudeAccountUuid`, `userEmail`, `userOrganizationUuid`, `isActive`, `claudeAccessTokenExpiresAt`, `lastUsedAt`, `rateLimitRequestsRemaining`, `rateLimitInputTokensRemaining`, `rateLimitOutputTokensRemaining`, `rateLimitStatus`, `createdAt`. Plaintext tokens SHALL NOT be present in the response.

#### Scenario: List does not leak tokens

- **WHEN** admin calls `GET /api/claude/accounts`
- **THEN** the response contains no plaintext access or refresh tokens

#### Scenario: List returns rate-limit cache fields

- **WHEN** admin calls `GET /api/claude/accounts` after some traffic
- **THEN** the rate-limit fields reflect the last persisted state for each account

### Requirement: Disable and re-enable Claude accounts

The system SHALL expose `PATCH /api/claude/accounts/{id}/disable` and `PATCH /api/claude/accounts/{id}/enable`. Disable SHALL set `accounts.is_active=false`, set `accounts.status` to a deactivated `AccountStatus` enum value, and record a `deactivation_reason`. Enable SHALL set `accounts.is_active=true` and `accounts.status` back to `AccountStatus.ACTIVE`. While disabled, the account SHALL NOT be selected by the load balancer.

#### Scenario: Disabled account is not selected

- **GIVEN** one Claude account is active and one is disabled
- **WHEN** the proxy handles a `/claude/v1/messages` request
- **THEN** only the active account is considered for selection

#### Scenario: Re-enable restores selection

- **WHEN** admin calls `PATCH /api/claude/accounts/{id}/enable` on a previously disabled account
- **THEN** the account's `is_active=true` and `status=ACTIVE`
- **AND** the account becomes eligible for selection on the next request

### Requirement: Auth guardian refreshes Claude access tokens

The system SHALL run a background pass (extending the existing auth guardian scheduler) that iterates Claude accounts with `claude_access_token_expires_at < now() + 600s` and calls `ClaudeAuthManager.rotate_claude_access_token` for each. Refresh failures with `invalid_grant` SHALL disable the account and emit a structured `claude.refresh.failed` log line. Transient refresh errors SHALL be retried with the same backoff the guardian already uses.

#### Scenario: Expired token is refreshed before the next request

- **GIVEN** a Claude account whose `claude_access_token_expires_at` is in the past
- **WHEN** the auth guardian scheduler pass runs
- **THEN** the account's `claude_access_token_encrypted` and `claude_access_token_expires_at` are updated

#### Scenario: invalid_grant disables the account

- **GIVEN** the auth guardian refreshes a Claude access token
- **AND** Anthropic responds with `invalid_grant`
- **WHEN** the refresh completes
- **THEN** the account's `is_active=false` and `status=DEACTIVATED`
- **AND** a structured `claude.refresh.failed` log line is emitted

### Requirement: Passthrough /claude/v1/messages

The system SHALL expose `POST /claude/v1/messages` that forwards Anthropic-native request bytes to the Anthropic API and returns the upstream response (streaming or non-streaming) verbatim, with no body translation. The route SHALL require an authenticated API key whose `provider_scope` includes `claude`. The route SHALL select a Claude account from the pool, inject the required auth headers, and write a `request_logs` row with `provider='claude'`.

#### Scenario: Non-streaming request passthrough returns upstream body verbatim

- **GIVEN** a healthy Claude account
- **WHEN** a client calls `POST /claude/v1/messages` with a non-streaming Anthropic request body
- **THEN** the proxy forwards the request to Anthropic
- **AND** the response body matches Anthropic's response bytes

#### Scenario: Streaming request passthrough forwards SSE events

- **GIVEN** a healthy Claude account
- **WHEN** a client calls `POST /claude/v1/messages` with `stream: true`
- **THEN** the proxy streams Anthropic SSE events to the client as they arrive
- **AND** the connection is closed only when Anthropic emits `message_stop`

#### Scenario: Codex-only key is rejected

- **GIVEN** an API key with `providerScope: "codex"`
- **WHEN** a client calls `POST /claude/v1/messages` with that key
- **THEN** the system returns 403

#### Scenario: 503 when no Claude accounts exist

- **GIVEN** no Claude accounts in the pool
- **WHEN** a client calls `POST /claude/v1/messages` with a valid Claude key
- **THEN** the system returns 503 with a JSON error envelope

### Requirement: 401 from Anthropic triggers rotate-and-retry once

When the Anthropic API returns HTTP 401 to a `POST /claude/v1/messages` proxy call, the system SHALL call `ClaudeAuthManager.rotate_claude_access_token(account, force=True)` once and retry the request exactly once. A second consecutive 401 SHALL propagate as a `ClaudeAuthError` to the client.

#### Scenario: 401 then 200 on retry

- **GIVEN** a Claude account with a stale access token
- **WHEN** the proxy request returns 401 from Anthropic
- **THEN** the access token is rotated
- **AND** the request is retried once
- **AND** the second response (typically 200) is returned to the client

#### Scenario: 401 twice propagates as auth error

- **GIVEN** a Claude account whose refresh token is invalid
- **WHEN** the proxy request returns 401 from Anthropic
- **AND** the rotated request also returns 401
- **THEN** the proxy surfaces a 502 to the client
- **AND** the account is marked unhealthy (existing cooldown mechanism)

### Requirement: Anthropic rate-limit headers populate account state

After every Claude request, the system SHALL parse `anthropic-ratelimit-requests-remaining`, `anthropic-ratelimit-requests-reset`, `anthropic-ratelimit-input-tokens-remaining`, `anthropic-ratelimit-input-tokens-reset`, `anthropic-ratelimit-output-tokens-remaining`, `anthropic-ratelimit-output-tokens-reset`, and `anthropic-ratelimit-status` from the upstream response and persist them to the corresponding `accounts.rate_limit_*` columns. When the response is a `429`, the system SHALL also set `accounts.status=RATE_LIMITED` and `accounts.reset_at=<future unix timestamp>`.

#### Scenario: Rate-limit headers update after a 200 response

- **WHEN** Anthropic returns 200 with `anthropic-ratelimit-requests-remaining: 42`
- **THEN** `accounts.rate_limit_requests_remaining` reflects 42 after the request completes

#### Scenario: 429 sets cooldown

- **WHEN** Anthropic returns 429
- **THEN** `accounts.status=RATE_LIMITED` and `accounts.reset_at` is set to a future timestamp
- **AND** `accounts.rate_limit_status` reflects the rejected/limited state

### Requirement: GET /claude/v1/models returns hardcoded catalog

The system SHALL expose `GET /claude/v1/models` returning a JSON object with a `data` array of Claude model descriptors in Anthropic's `models` shape. The model id list SHALL be hardcoded in `app/modules/claude/models_catalog.py` and SHALL include the current set of Max/Pro/Team-eligible Claude model ids at the time of the change. Deprecated model ids SHALL NOT be present in the catalog.

#### Scenario: Models endpoint returns the catalog

- **WHEN** a client calls `GET /claude/v1/models`
- **THEN** the response includes at least one Claude model descriptor
- **AND** no deprecated model ids appear in the response

### Requirement: Soft-delete preserves request history

When a Claude account is disabled, existing `request_logs` rows that reference its `account_id` SHALL remain readable from the dashboard. The `request_logs.account_id` foreign key already uses `ondelete="SET NULL"` in `app/db/models.py`, so historical rows are preserved by default and the soft-disable flow does not need to alter the schema.

#### Scenario: Disabled Claude account keeps request log history

- **GIVEN** a Claude account with `request_logs` history
- **WHEN** the account is disabled via the dashboard
- **THEN** the request logs remain in the database with `account_id` unchanged
- **AND** the dashboard request-log view still shows them, tagged with the disabled account id

### Requirement: Dashboard Claude accounts tab

The SPA dashboard SHALL expose a "Claude Accounts" sidebar entry alongside "Accounts". The tab SHALL show a list of Claude accounts with the same action affordances as the Codex list (disable, enable), an "Add Claude account" button that opens a dialog with the fields from the manual-add requirement, and a usage card per account showing the current rate-limit cache and today's `request_logs.tokens_total`.

#### Scenario: Add Claude account dialog submits to the admin endpoint

- **WHEN** the operator clicks "Add Claude account" and submits valid fields
- **THEN** the dashboard calls `POST /api/claude/accounts`
- **AND** on success, the new account appears in the list

#### Scenario: Empty state when no Claude accounts exist

- **GIVEN** no Claude accounts in the pool
- **WHEN** the operator navigates to the Claude Accounts tab
- **THEN** the tab shows an empty-state message ("Add your first Claude account")
- **AND** the "Add Claude account" button is visible

#### Scenario: Usage card reflects current rate-limit state

- **WHEN** the operator opens a Claude account's usage card
- **THEN** the card displays `rate_limit_requests_remaining`, `rate_limit_input_tokens_remaining`, `rate_limit_output_tokens_remaining`, `rate_limit_status`, and today's tokens total

### Requirement: i18n strings for the Claude tab

The SPA SHALL ship `en` and `zh-CN` translations for the new Claude accounts tab: tab title, add-button label, form labels, error messages, and empty state.

#### Scenario: English locale renders Claude tab strings

- **WHEN** the operator opens the Claude Accounts tab in English locale
- **THEN** all UI text is rendered in English (no fallback key visible)

#### Scenario: zh-CN locale renders Chinese strings

- **WHEN** the operator opens the Claude Accounts tab in zh-CN locale
- **THEN** all UI text is rendered in Simplified Chinese

### Requirement: ProxyService is not modified by this change

The `app/modules/proxy/service.py` ProxyService god object SHALL NOT be modified by this change. The Claude proxy logic SHALL live in `app/modules/claude/service.py` and SHALL communicate with the load balancer and request-log subsystems through the same interfaces the existing ProxyService uses.

#### Scenario: Architecture ratchets remain green

- **WHEN** `make architecture-check` runs against this change
- **THEN** the ProxyService line count, method span, and cross-domain dependency ratchets are unchanged
- **AND** the check exits zero

### Requirement: Verification of Anthropic OAuth contract

Before this change is declared ready, the implementation phase SHALL verify against a real Claude Code CLI token exchange in a sandbox:
- Anthropic OAuth refresh endpoint URL and request/response shape
- Required Anthropic API header set for OAuth-authenticated requests
- Whether Anthropic rotates the refresh token on each access-token refresh
- Exact names and semantics of Anthropic rate-limit response headers

The verification findings SHALL be recorded in `openspec/changes/add-claude-oauth-pool/notes.md` and referenced from the PR description.

#### Scenario: Verification notes are committed

- **WHEN** this change is marked ready for review
- **THEN** `openspec/changes/add-claude-oauth-pool/notes.md` exists
- **AND** it documents each of the four verification bullets above
- **AND** it references the sources used for verification