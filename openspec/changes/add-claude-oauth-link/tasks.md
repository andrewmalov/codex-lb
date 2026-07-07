# Tasks

## 1. Backend — Settings

- [ ] 1.1 Add `claude_oauth_client_id`, `claude_oauth_redirect_uri`, `claude_oauth_scopes`, `claude_oauth_flow_ttl_seconds`, `claude_oauth_authorization_code_max_length` to `app/core/config/settings.py` with the defaults from the design doc.
- [ ] 1.2 Document the new env vars in `.env.example` with a one-line comment each.
- [ ] 1.3 Wire `claude_oauth_authorize_endpoint` (already declared) into the URL builder — the field exists but is currently unused.

## 2. Backend — Anthropic OAuth client

- [ ] 2.1 In `app/core/clients/anthropic/oauth.py`, add `ClaudeAuthorizationCodeResult` dataclass (sibling of `ClaudeRefreshResult`).
- [ ] 2.2 Add `ClaudeOAuthClient.exchange_authorization_code(*, code, code_verifier, redirect_uri)` method.
- [ ] 2.3 Mirror the existing `refresh()` error model: `invalid_grant` → `ClaudeAuthError`, 5xx → `ClaudeUpstreamError`, parse fail → `ClaudeAPIError`. `id_token` is allowed to be `None` (do not raise).
- [ ] 2.4 Unit tests for `exchange_authorization_code` happy path, `invalid_grant`, 5xx, missing `id_token` tolerated, malformed body.

## 3. Backend — Claude OAuth module

- [ ] 3.1 Create `app/modules/claude/oauth/__init__.py` (empty package marker).
- [ ] 3.2 Create `app/modules/claude/oauth/schemas.py` with `ClaudeOauthStartRequest`, `ClaudeOauthStartResponse`, `ClaudeOauthStatusResponse`, `ClaudeOauthCallbackRequest`, `ClaudeOauthCallbackResponse` (Pydantic, `DashboardModel` base, camelCase aliases).
- [ ] 3.3 Create `app/modules/claude/oauth/tokens.py` with `generate_pkce_pair()` and `decode_id_token(jwt) -> ClaudeOauthClaims` (JSON-decode only, no signature verification; best-effort claim mapping with explicit fallback keys per design doc).
- [ ] 3.4 Create `app/modules/claude/oauth/service.py` with `ClaudeOAuthService`:
  - `start_oauth() -> (flow_id, authorization_url, expires_in_seconds, callback_instructions, redirect_uri)` — generates state token and PKCE pair, supersedes any prior pending flow with `error: superseded`, returns response.
  - `oauth_status(flow_id) -> ClaudeOauthStatusResponse` — read-only state lookup.
  - `complete_oauth(flow_id, code, state) -> ClaudeOauthCallbackResponse` — validates CSRF state, checks flow is pending, exchanges code via `ClaudeOAuthClient.exchange_authorization_code`, decodes `id_token`, calls `ClaudeAuthManager.add_claude_account_from_oauth`, marks flow as success with `account_id`, returns the public account payload.
  - `ClaudeOAuthFlow` dataclass with `flow_id`, `state_token`, `code_verifier`, `status`, `error_code`, `error_message`, `started_at`, `finished_at`, `account_id`.
  - In-memory `ClaudeOAuthStateStore` keyed by `flow_id` and by `state_token` (for fast CSRF lookup); only one active flow at a time; TTL eviction (default 600s).
- [ ] 3.5 Create `app/modules/claude/oauth/api.py` with FastAPI router:
  - `POST /api/claude/oauth/start` (write access required)
  - `GET  /api/claude/oauth/status` (no write access required)
  - `POST /api/claude/oauth/callback` (write access required)
  - Dashboard session + error format dependencies; map service errors to documented `error_code` and HTTP status (see design §error code reference).
- [ ] 3.6 Wire the new router in `app/main.py` alongside the existing Claude admin router.

## 4. Backend — Auth manager extension

- [ ] 4.1 Add `add_claude_account_from_oauth(*, access_token, refresh_token, expires_in, id_token_claims) -> str` method to `ClaudeAuthManager` that delegates to the existing `add_claude_account(...)` with claims-derived values.
- [ ] 4.2 Unit test: round-trip encryption, propagation of `ClaudeAccountAlreadyExists`, no double-validation of already-typed claim fields.

## 5. Frontend — Dialog + wiring

- [ ] 5.1 Create `frontend/src/components/claude/AddClaudeAccountOAuthDialog.tsx` (multi-step: idle → start → paste code → success/error). Reuse existing dialog primitives and `<ClaudeAccountList>` row for the success step.
- [ ] 5.2 Add a new "Add via OAuth" button to `ClaudeAccountList` next to the existing "Add manually" button.
- [ ] 5.3 Status polling is NOT required (synchronous paste flow); status endpoint exists only for page-refresh recovery. Document this in the dialog help text.
- [ ] 5.4 Add zod schemas `ClaudeOauthStartResponseSchema`, `ClaudeOauthCallbackRequestSchema`, `ClaudeOauthStatusResponseSchema` to `frontend/src/lib/schemas.ts`.
- [ ] 5.5 Add typed API client methods (likely in `frontend/src/lib/api.ts` or co-located hooks): `startClaudeOauth()`, `getClaudeOauthStatus(flowId)`, `submitClaudeOauthCallback(payload)`.
- [ ] 5.6 i18n keys in `frontend/src/locales/en.json` and `frontend/src/locales/zh-CN.json` (per design doc §i18n).

## 6. Tests — Unit

- [ ] 6.1 `tests/unit/test_claude_oauth_tokens.py`: PKCE generation, S256 verification, `decode_id_token` happy path, all claim-mapping fallbacks, missing `id_token` returns `None` (not an exception), malformed JWT raises typed error.
- [ ] 6.2 `tests/unit/test_claude_oauth_service.py`: state transitions, single-flight supersede, TTL expiry, CSRF state mismatch, full stub-Anthropic happy path, every documented `error_code`.
- [ ] 6.3 `tests/unit/test_claude_oauth_api.py`: HTTP envelope (status codes, error codes), auth dependency enforcement, request validation, response shape.
- [ ] 6.4 `tests/unit/test_anthropic_oauth_client.py` (extend existing): `exchange_authorization_code` happy path, `invalid_grant`, 5xx, missing `id_token` tolerated.
- [ ] 6.5 `tests/unit/test_claude_auth_manager_oauth.py` (extend): `add_claude_account_from_oauth` round-trip, `ClaudeAccountAlreadyExists` propagation.

## 7. Tests — Integration

- [ ] 7.1 `tests/integration/test_claude_oauth_flow.py`: full happy path — start → mock Anthropic → callback → row created → list endpoint shows new account.
- [ ] 7.2 `tests/integration/test_claude_oauth_errors.py`: `state_mismatch`, `flow_not_found`, `flow_expired` (mock time), `account_already_exists`, `id_token_missing`, `invalid_grant` propagation.
- [ ] 7.3 `tests/integration/test_claude_oauth_manual_paste_unchanged.py`: regression guard — `POST /api/claude/accounts` (manual) still works after the change.

## 8. OpenSpec artifacts

- [ ] 8.1 `openspec/changes/add-claude-oauth-link/specs/claude-oauth-pool/spec.md` — `## ADDED Requirements` for "Claude account add via OAuth", "OAuth flow state machine is single-in-flight", "OAuth callback validates CSRF state token", "id_token claims populate claude account fields", each with `#### Scenario:` blocks.
- [ ] 8.2 `openspec/changes/add-claude-oauth-link/design.md` — full design write-up of the 6 sections agreed during brainstorming.
- [ ] 8.3 `openspec/changes/add-claude-oauth-link/context.md` — operational notes (multi-replica caveat, sticky-session TODO, redirect URI verification sources, "use manual paste fallback" guidance).
- [ ] 8.4 `openspec/changes/add-claude-oauth-link/proposal.md` (this file) — present.
- [ ] 8.5 `openspec/changes/add-claude-oauth-link/tasks.md` (this file) — present.
- [ ] 8.6 `openspec validate add-claude-oauth-link --strict --no-interactive` — passes.

## 9. Final verification

- [ ] 9.1 `make lint` — clean.
- [ ] 9.2 `make typecheck` — no new diagnostics (pre-existing 175 may remain).
- [ ] 9.3 `make test-unit` — clean.
- [ ] 9.4 `make test-integration-core` — clean.
- [ ] 9.5 `make migration-check` — clean (no migrations in this change, but run the guard).
- [ ] 9.6 `make package` — clean.
- [ ] 9.7 `make architecture-check` — clean (no edits to `app/modules/proxy/service.py`).
- [ ] 9.8 `openspec validate add-claude-oauth-link --strict --no-interactive` — clean.
- [ ] 9.9 PR description references `add-claude-oauth-pool` as the predecessor and explains why this is a separate change.
