## Why

`codex-lb` already pools Claude Max/Pro/Team OAuth tokens (change `add-claude-oauth-pool`) but the **only** way to add a Claude account is `POST /api/claude/accounts`, which requires the operator to manually extract `access_token`, `refresh_token`, `expires_in_seconds`, scopes, and identity fields from the Anthropic OAuth response and paste them into the dashboard. The original proposal explicitly deferred PKCE-based authorization code flow to a follow-up change.

For comparison, the Codex provider already has a complete OAuth flow with browser redirect, device code, and manual callback — that flow is the reference pattern. This change brings Claude to parity: operators can click "Add Claude account via OAuth" in the dashboard, get a generated authorization URL, complete the consent on `claude.ai`, and paste the resulting code back into the dashboard. The proxy performs the authorization-code exchange (with PKCE), parses the `id_token` for identity claims, and persists the new account through the same encryption + insert path used by the manual paste endpoint.

The flow does **not** require a local HTTP callback server on a fixed port. The redirect target is `https://console.anthropic.com/oauth/code` (Claude Code's OOB-style code page), so the user simply copies the code from the rendered page and pastes it back. This avoids port conflicts with the Codex flow on `1455` and works the same way in Docker, headless, and remote-access setups.

## What Changes

- **New module** `app/modules/claude/oauth/` (isolated from `app/modules/oauth/`) implementing an authorization_code + PKCE flow with copy-paste code entry, including a single-in-flight state machine, CSRF state-token validation, and flow TTL.
- **New endpoints** under `/api/claude/oauth/`:
  - `POST /api/claude/oauth/start` — generate authorization URL with PKCE challenge and state token.
  - `GET  /api/claude/oauth/status` — read the current flow status.
  - `POST /api/claude/oauth/callback` — exchange the pasted code for tokens and persist the new account.
- **Extended** `ClaudeAuthManager` with a thin `add_claude_account_from_oauth(...)` wrapper that reuses the existing encryption + insert path.
- **Extended** `app/core/clients/anthropic/oauth.py` with a new `exchange_authorization_code(code, code_verifier, redirect_uri)` method (sibling of the existing `refresh(...)`).
- **New settings** in `app/core/config/settings.py` for the OAuth client id, redirect URI, scopes, and flow TTL.
- **New frontend dialog** `AddClaudeAccountOAuthDialog` and i18n strings (en + zh-CN), wired into the existing Claude accounts tab alongside the existing manual-paste dialog.
- **Zod schemas** in `frontend/src/lib/schemas.ts` for the new request/response shapes.
- **Tests** at unit and integration level covering the happy path, all documented error codes, and a regression guard for the unchanged manual-paste endpoint.

The existing `POST /api/claude/accounts` (manual paste) is **kept unchanged** as a fallback when the OAuth flow cannot extract identity.

## Capabilities

### Modified Capabilities

- `claude-oauth-pool`: Extend the existing Claude OAuth pool capability with the OAuth-based add flow (authorization code + PKCE + copy-paste code entry). The existing "Manual Claude account add" requirement stays; a new "Claude account add via OAuth" requirement is added.

## Impact

- **Database**: No schema changes. The OAuth add reuses the existing `accounts` columns added by `add-claude-oauth-pool` (`provider`, `claude_account_uuid`, `claude_access_token_encrypted`, `claude_refresh_token_encrypted`, `claude_access_token_expires_at`, `claude_scopes`, `claude_user_email`, `claude_user_organization_uuid`).
- **Backend Python**: New module `app/modules/claude/oauth/` (~400 lines); one new method on `ClaudeAuthManager`; one new method on `ClaudeOAuthClient`; one new method on `ClaudeProxyService`'s sibling service if any; five new settings keys. No edits to `app/modules/proxy/service.py` (ADR-0001 preserved). No edits to `app/modules/oauth/*` (Codex flow untouched).
- **Frontend**: New dialog component, new i18n keys, new zod schemas. No changes to existing components beyond a new button alongside the existing "Add manually" entry.
- **Settings surface**: New env vars: `CODEX_LB_CLAUDE_OAUTH_CLIENT_ID`, `CODEX_LB_CLAUDE_OAUTH_REDIRECT_URI`, `CODEX_LB_CLAUDE_OAUTH_SCOPES`, `CODEX_LB_CLAUDE_OAUTH_FLOW_TTL_SECONDS`. All have safe defaults.
- **Multi-replica**: The flow state is process-local (matches the existing Codex flow). If a user starts the flow on replica A and pastes the code on replica B, the callback returns 404 `flow_not_found`. Documented in `context.md`; sticky-session routing or shared state is out of scope.
- **Verification**: The Anthropic OAuth endpoint URLs and `id_token` claim names are verified against public sources; the redirect URI `https://console.anthropic.com/oauth/code` is verified against the Claude Code client's behavior. Live verification of the exchange against a real Anthropic account is out of scope and stays an operator-side step.
- **No breaking changes**: The existing `POST /api/claude/accounts` endpoint is unchanged; the OAuth endpoints are additive.
