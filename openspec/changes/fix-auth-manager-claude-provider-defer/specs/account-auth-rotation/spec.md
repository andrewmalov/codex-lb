# account-auth-rotation Specification (delta)

## ADDED Requirements

### Requirement: AuthManager rotation is provider-scoped

`app.modules.accounts.auth_manager.AuthManager.refresh_account` MUST
short-circuit when called for an `Account` with
`Account.provider == "claude"`. The short-circuit:

- Returns the input `Account` instance unchanged.
- Does NOT call `app.core.auth.refresh.refresh_access_token`.
- Does NOT call `AccountsRepositoryPort.update_status`.
- Does NOT call `AccountsRepositoryPort.update_tokens`.
- Does NOT call `mark_account_routing_unavailable`.

Rationale: Claude OAuth rotation is owned by
`app.core.auth.guardian.AuthGuardianScheduler`, which delegates to
`app.modules.claude.auth_manager.ClaudeAuthManager.rotate_claude_access_token`.
The Codex-flavored `refresh_token_encrypted` column on a Claude row
holds the literal placeholder `"claude"` (encrypted) — set by
`ClaudeAuthManager.add_claude_account:279-281` because the table
constraint `ck_accounts_claude_rt_required` only requires the
`claude_refresh_token_encrypted` column to be populated. Decrypting that
column and posting it to the Codex OAuth endpoint returns
`400 invalid_grant`, which the existing failure branch surfaces as a
permanent failure and flips the account to `status='reauth_required'`
within ~5 seconds of the OAuth callback — see
`openspec/changes/fix-auth-manager-claude-provider-defer/context.md`
for the 2026-07-17 trace on `claude-test.bezproblem.vip`.

#### Scenario: Codex row still rotates via the Codex AuthManager

- **GIVEN** an `Account` with `provider == "codex"` (default) and a
  real `refresh_token_encrypted` value
- **WHEN** `AuthManager.refresh_account(account)` is called
- **THEN** `refresh_access_token` is invoked
- **AND** the row's `access_token_encrypted`, `refresh_token_encrypted`,
  `id_token_encrypted`, `last_refresh`, `plan_type`, and `email` are
  updated on success.

#### Scenario: Claude row is left alone by the Codex AuthManager

- **GIVEN** an `Account` with `provider == "claude"`,
  `refresh_token_encrypted = encrypt("claude")` (placeholder), and
  `claude_refresh_token_encrypted = encrypt(<real Claude refresh token>)`
- **WHEN** `AuthManager.refresh_account(account)` is called
- **THEN** `refresh_access_token` is NOT called
- **AND** `AccountsRepositoryPort.update_status` is NOT called
- **AND** `AccountsRepositoryPort.update_tokens` is NOT called
- **AND** the input `Account` is returned unchanged
- **AND** the row's `status` remains `active` (no `reauth_required`
  flip).

#### Scenario: Codex failure path still escalates to reauth_required

- **GIVEN** an `Account` with `provider == "codex"` whose
  `refresh_token_encrypted` has been revoked upstream
- **WHEN** `AuthManager.refresh_account(account)` is called and
  `refresh_access_token` returns `400 invalid_grant`
- **THEN** the existing failure branch runs
- **AND** `AccountsRepositoryPort.update_status(account.id,
  REAUTH_REQUIRED, reason)` is called
- **AND** the row's `status` is `reauth_required` in the DB.

This scenario guards against an over-correction: the short-circuit
must NOT apply to Codex rows.

### Requirement: Dashboard and usage endpoints MUST tolerate Claude rows

Dashboard endpoints and the usage refresh scheduler MUST tolerate
Claude rows without flipping them to `reauth_required`. Specifically:

- `app.modules.accounts.service.AccountsService.get_usage_reset_credits`
  and `_consume_usage_reset_credit` may surface
  `AccountUsageResetCreditsUnavailableError` for Claude rows (the
  Anthropic OAuth API has no equivalent of Codex's `/v1/usage`
  endpoint).
- `app.modules.usage.updater` (usage refresh scheduler) may skip
  Claude rows with a log line and continue.
- `app.modules.accounts.service` account probe MUST return a result
  without mutating the row's `status`.

For Claude rows, the failure mode is rendered as a 409 Conflict on
the dashboard endpoint and a "skipped" line in the usage-refresh
logs — NOT as a status flip. The provider-scoped short-circuit in
`AuthManager.refresh_account` is what guarantees this invariant; no
separate guard is needed in the call sites because the short-circuit
runs before any DB write.

#### Scenario: Claude account survives the first dashboard poll

- **GIVEN** a Claude account just added via OAuth-link with
  `status='active'`
- **WHEN** the dashboard polls
  `GET /api/accounts/{id}/usage-reset-credits`
- **THEN** the request returns `409 Conflict`
  (`AccountUsageResetCreditsUnavailableError`)
- **AND** the account's `status` remains `active` in the DB (no
  `reauth_required` flip from a Codex-flavored refresh attempt).
