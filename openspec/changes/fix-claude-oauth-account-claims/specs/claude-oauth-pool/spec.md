# claude-oauth-pool Specification (delta)

## MODIFIED Requirements

### Requirement: Claude account add via OAuth

The system SHALL expose `POST /api/claude/oauth/start`, `GET /api/claude/oauth/status`,
and `POST /api/claude/oauth/callback` to add a Claude account through an
authorization-code flow with PKCE and copy-paste code entry. The system SHALL:

- Generate an authorization URL with `response_type=code`, `code_challenge`
  (method `S256`), a server-generated `state` token, and a `code=true` query
  parameter as the first parameter, selecting Anthropic's OOB code-display
  flow.
- The default `claude_oauth_authorize_endpoint` SHALL be
  `https://claude.com/cai/oauth/authorize` and the default
  `claude_oauth_redirect_uri` SHALL be
  `https://platform.claude.com/oauth/code/callback`. These are the values
  Anthropic accepts for the public Claude Code OAuth client
  `9d1c250a-e61b-44d9-88ed-5944d1962f5e`. Operators MAY override either via
  env var (`CODEX_LB_CLAUDE_OAUTH_AUTHORIZE_ENDPOINT`,
  `CODEX_LB_CLAUDE_OAUTH_REDIRECT_URI`).
- Return the `state` token in the `/start` response payload (under a
  `stateToken` field) so the dashboard dialog can submit it unchanged on the
  `/callback` request.
- Require the user to paste the authorization response into
  `POST /api/claude/oauth/callback`. The pasted value MAY be in either
  format:
  - A bare `code` (no `#` separator). The system forwards it as-is to the
    token exchange.
  - A `code#state` value where the second segment is the state token emitted
    by `/start`. The system MUST split on the first `#`, validate the state
    segment equals the stored `flow.state_token` via
    `secrets.compare_digest`, and use only the code segment for the exchange.
    A mismatched state segment MUST reject with HTTP 400 and
    `error_code: state_mismatch`.
- Exchange the code at the Anthropic OAuth token endpoint using the stored
  PKCE `code_verifier`, the configured `redirect_uri`, the configured
  `client_id`, and the `state` value forwarded from `flow.state_token`.
- Parse the response. Account identity MAY come from either:
  - An OIDC `id_token` (JWT) whose payload carries `claude_account_uuid`
    (alias: `account_id`, namespaced `https://api.anthropic.com/account_id`,
    or UUID-shaped `sub`).
  - The response body's `account.uuid` plus `account.email_address` plus
    `organization.uuid` (Anthropic's actual shape for the public client
    — the token endpoint does NOT return an `id_token`).
  The system SHALL accept either source. The `error_code: id_token_missing`
  response SHALL be raised only when **both** sources are absent.
- Persist the new account via the same encryption + insert path used by the
  existing `POST /api/claude/accounts` (manual paste) endpoint.
- Reject the callback when the supplied `state` (or the `state` segment of
  `code#state`) does not match the stored `state_token` (HTTP 400,
  `error_code: state_mismatch`).
- Reject the callback when the `code` is empty at request validation time
  (HTTP 422 Pydantic envelope).
- Reject a duplicate `claude_account_uuid` with HTTP 409
  (`error_code: account_already_exists`).
- Support at most one in-flight flow at a time. A new `POST /api/claude/oauth/start`
  SHALL transition any prior `pending` flow to `error` with
  `error_code: superseded`, and SHALL create a new flow with a fresh state
  token and PKCE pair.
- Consider a pending flow expired when
  `started_at + claude_oauth_flow_ttl_seconds < now()` at the moment of a
  status or callback request. Past that point the callback SHALL return HTTP
  410 (`error_code: flow_expired`) and the status endpoint SHALL report
  `status: error` with `error_code: flow_expired`.

#### Scenario: Operator pastes `code#state` from Anthropic's OOB page

- **GIVEN** a `pending` flow with `state_token = S` and `code_verifier = V`
- **WHEN** the operator pastes `"<code>#<S>"` (Anthropic's OOB display
  format) and the token endpoint returns `200` with
  `{"account": {"uuid": "U", "email_address": "E"}, "organization": {"uuid": "O"}}`
  and no `id_token`
- **THEN** the callback splits the paste on `#`, validates the state half
  against `S`, exchanges with `code=<code>`, `code_verifier=V`, `state=S`
- **AND** constructs `ClaudeOauthClaims{claude_account_uuid: "U",
  user_email: "E", user_organization_uuid: "O"}`
- **AND** persists the account row and returns 200.

#### Scenario: Token endpoint returns id_token shape (backward compat)

- **GIVEN** a `pending` flow
- **WHEN** the token endpoint returns `200` with a JWT `id_token` whose
  payload carries `claude_account_uuid` (legacy Anthropic deployments)
- **THEN** the callback decodes the id_token, extracts
  `ClaudeOauthClaims`, and persists the account row the same way.

#### Scenario: `code#state` with mismatched state half

- **GIVEN** a `pending` flow with `state_token = S`
- **WHEN** the operator pastes `"<code>#<S_other>"`
- **THEN** the callback returns HTTP 400 with `error_code: state_mismatch`
  and does NOT call the token endpoint.

## ADDED Requirements

### Requirement: Token-exchange response handling

The `ClaudeOAuthClient.exchange_authorization_code` method SHALL parse the
200 token-exchange response and surface the following fields:

- `access_token` (string, required)
- `refresh_token` (string, required)
- `expires_in` (integer seconds, required)
- `scope` (string or null, optional)
- `id_token` (string or null, optional) — JWT form when Anthropic returns it
- `account_uuid` (string or null) — from `account.uuid` in the response body
- `account_email` (string or null) — from `account.email_address`
- `organization_uuid` (string or null) — from `organization.uuid`
- `organization_name` (string or null) — from `organization.name`

The OAuth client MUST NOT raise on a missing `id_token`. The caller
(`ClaudeOAuthService.complete_oauth`) is responsible for selecting between
the id_token-derived and account-derived sources of identity. Both
`account` and `organization` MAY be absent, present-but-not-a-dict, or null;
the client MUST treat any of these as "no identity payload" without raising.

#### Scenario: Anthropic actual-shape response (no id_token)

- **GIVEN** the token endpoint returns
  ```json
  {
    "access_token": "sk-ant-oat01-...",
    "refresh_token": "sk-ant-ort01-...",
    "expires_in": 28800,
    "scope": "user:inference user:profile",
    "token_uuid": "...",
    "refresh_token_expires_in": 2502728,
    "organization": {"uuid": "O", "name": "..."},
    "account": {"uuid": "U", "email_address": "..."}
  }
  ```
- **THEN** `ClaudeAuthorizationCodeResult.id_token is None`,
  `account_uuid == "U"`, `account_email == "..."`,
  `organization_uuid == "O"`, `organization_name == "..."`.
- **AND** the OAuth client returns this result (does NOT raise).

#### Scenario: Backward-compat response with id_token

- **GIVEN** the token endpoint returns an `id_token` JWT and no `account` /
  `organization` keys
- **THEN** `ClaudeAuthorizationCodeResult.id_token` is the JWT string,
  `account_uuid is None`, `account_email is None`,
  `organization_uuid is None`.

#### Scenario: Malformed `account` / `organization` keys

- **GIVEN** the token endpoint returns
  `{"account": null, "organization": "not-a-dict"}` along with valid
  tokens
- **THEN** the OAuth client returns a result with the new fields `None`
  (no exception).

### Requirement: Callback diagnostic logging

`ClaudeOAuthService.complete_oauth` MUST emit one structured
`logger.warning("claude.oauth.flow.callback.diagnostic", extra={...})` at
the moment of token exchange with the fields: flow_id, code length, code
head/tail, submitted state prefix, flow state prefix, states_match (bool).

When the `id_token_missing` path is reached (no `id_token` AND no
`account.{uuid, email_address}`), the service MUST emit
`logger.error("claude.oauth.flow.id_token_missing", extra={...})` including
flow_id and the raw response body excerpt (first 2KB).

`extra={...}` is the contract; production deployments use
`CODEX_LB_LOG_FORMAT=json`, which preserves structured fields.

#### Scenario: Diagnostic emitted on every callback

- **WHEN** the operator submits `/api/claude/oauth/callback`
- **THEN** exactly one `claude.oauth.flow.callback.diagnostic` warning is
  emitted with all the documented fields.

#### Scenario: id_token_missing log captures raw body

- **WHEN** Anthropic returns 200 with neither `id_token` nor `account.uuid`
- **THEN** the `claude.oauth.flow.id_token_missing` log includes
  `raw_body` containing the response body excerpt.
- **AND** the dashboard surfaces `error_code: id_token_missing`.