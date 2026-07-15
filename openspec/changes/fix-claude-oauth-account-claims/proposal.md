# fix-claude-oauth-account-claims

## Why

Operators who follow the documented "Add Claude account via OAuth" dialog flow
on the dashboard hit `invalid_grant` or `id_token_missing` on every attempt,
making the OAuth-link account-add path unusable. This blocks the entire
claude-oauth-pool capability on real Anthropic accounts. The diagnosis
(2026-07-15, see `openspec/changes/diagnose-claude-oauth-add-blocker/`) shows
two distinct spec/code mismatches with Anthropic's actual OAuth2 surface
for the public client `9d1c250a-e61b-44d9-88ed-5944d1962f5e`:

1. **`code#state` paste format.** Anthropic's OOB authorize page
   (`https://platform.claude.com/oauth/code/callback`) renders the
   authorization response as `code#state` with a literal `#` separator
   (the same convention used by Claude Code CLI and the
   `querymt/anthropic-auth` Rust library's `parse_code_and_state`). Codex-lb's
   dialog asks the user to paste "the code"; users paste `code#state` (≈ 92
   characters: 44-char code + `#` + 47-char state). The full string is
   sent to `https://platform.claude.com/v1/oauth/token` as the `code` field;
   Anthropic returns `invalid_grant "Invalid 'code' in request."`.

2. **Anthropic does not return `id_token` for the code#state flow.** Codex-lb's
   spec required extracting account identity from a JWT `id_token`. The
   public client's actual response carries account identity in plain JSON
   fields `account.uuid`, `account.email_address`, and
   `organization.{uuid,name}`. Codex-lb raises `id_token_missing` → HTTP 400,
   rejecting the otherwise-valid exchange.

`add-claude-oauth-pool` shipped before either was known; PR #16 corrected
the authorize endpoint; PR #26 added the `state` parameter the token
endpoint requires. This change closes the remaining gap.

## What changes

### Capability: `claude-oauth-pool`

1. **`ClaudeAuthorizationCodeResult` extended** with `account_uuid`,
   `account_email`, `organization_uuid`, `organization_name` fields
   extracted from the token response body. Backward-compatible — existing
   `id_token` field unchanged.

2. **`ClaudeOAuthService.complete_oauth` parses `code#state` paste format.**
   If the submitted `code` contains a `#`, split on it, validate the state
   half against `flow.state_token`, and use only the code half for the
   exchange. Plain codes (no `#`) flow through unchanged. Reject
   `state_mismatch` (400) when the state half doesn't match.

3. **Account identity construction prefers Anthropic's actual response shape.**
   Build `ClaudeOauthClaims` from `id_token` when present, otherwise from
   `account.uuid` + `account.email_address` + `organization.uuid` + scope.
   `id_token_missing` is now raised only when **both** `id_token` and the
   `account.*`/`organization.*` fields are absent.

4. **Diagnostic logging** — the previously ad-hoc in-container diagnostic
   (logger.warning with `%s`-style fields covering flow_id, code head/tail,
   state prefix match, verifier head + SHA-256 challenge prefix, raw
   Anthropic response body excerpt on `id_token_missing`) moves into the
   codebase as a single, narrowly-scoped logger.warning at the start of
   `complete_oauth` and on `id_token_missing`. Uses `extra={}` because
   `CODEX_LB_LOG_FORMAT=json` is the production default; the text
   formatter fallback still prints the diagnostic message verbatim.

### Non-goals

- **Signature verification on `id_token`.** Out of scope per
  `add-claude-oauth-link/context.md §Signature verification` (no JWKS client).
- **Account profile fetch from `/api/oauth/profile`.** Not needed — the token
  response already carries `account.{uuid, email_address}`.
- **Switching to `?code=true` redirect flow with a local callback server.**
  Codex-lb's runtime can be remote (Docker, behind a reverse proxy); a local
  callback server is rejected by `add-claude-oauth-link/context.md §Why no
  local callback server`. OOB paste remains the user-facing flow.
- **Allowing codes pasted from sources other than Anthropic's OOB page.**
  Anything matching the `code#state` shape is accepted; bare codes also
  accepted. URLs rejected as `state_mismatch` (would carry a different state).

## Success criteria

- An operator running through the documented Add Claude account flow can
  paste the OOB `code#state` string and complete account add without
  touching `.env`, hot-patches, or shell workarounds.
- Regression coverage in `tests/unit/test_anthropic_oauth_exchange.py` and
  `tests/unit/test_claude_oauth_service.py` for: (a) id_token-shaped
  Anthropic response (kept for backward compat), (b) account-shaped
  Anthropic response (new), (c) `code#state` paste (new),
  (d) `code#state` with wrong state half (new).
- `openspec validate --strict` clean.