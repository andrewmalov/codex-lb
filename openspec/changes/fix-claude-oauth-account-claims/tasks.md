# Tasks

## 1. Extend `ClaudeAuthorizationCodeResult` with account-shape fields

**File:** `app/core/clients/anthropic/oauth.py`

- Add fields to the frozen dataclass: `account_uuid: str | None = None`,
  `account_email: str | None = None`, `organization_uuid: str | None = None`,
  `organization_name: str | None = None`.
- Default values preserve backward compatibility with existing
  `ClaudeAuthorizationCodeResult(id_token=...)` callers and existing test
  fixtures.

**Acceptance:** existing tests in `tests/unit/test_anthropic_oauth_exchange.py`
still pass without modification.

## 2. Populate the new fields in `_parse_exchange_success_body`

**File:** `app/core/clients/anthropic/oauth.py`

- After parsing `access_token`/`refresh_token`/`expires_in`/`scope`/`id_token`,
  extract `account = body.get("account") or {}` and
  `organization = body.get("organization") or {}`.
- Map `account.get("uuid")`, `account.get("email_address")`,
  `organization.get("uuid")`, `organization.get("name")` onto the new fields.
- Non-dict values for `account`/`organization` are treated as empty.

**Acceptance:** when the response carries `account.uuid` and
`account.email_address` and no `id_token`, `result.id_token is None` AND
`result.account_uuid is not None` AND `result.account_email is not None`.

## 3. Parse `code#state` in `complete_oauth`

**File:** `app/modules/claude/oauth/service.py`

- Immediately before the existing `# DIAGNOSTIC` comment block (or
  equivalently, right after the `secrets.compare_digest(state, …)` state
  check on line 294), add a block:
  ```python
  if "#" in code:
      code_part, state_part = code.split("#", 1)
      state_part = state_part.strip()
      if not secrets.compare_digest(state_part, flow.state_token or ""):
          raise ClaudeOauthFlowError(
              "state_mismatch",
              "code#state state does not match the stored token.",
              http_status=400,
          )
      code = code_part.strip()
  ```
- The `state` parameter passed to `complete_oauth` continues to be the
  flow's `state_token`; the state half of `code#state` is validated against
  it, then discarded.

**Acceptance:** plain `code` (no `#`) reaches `exchange_authorization_code`
unchanged; `code#state` with matching state reaches it with the state
half stripped; `code#state` with mismatched state raises `state_mismatch` at
the same `http_status=400` as the existing state check.

## 4. Build `ClaudeOauthClaims` from either `id_token` or account fields

**File:** `app/modules/claude/oauth/service.py`

- Replace the existing `if not result.id_token: … id_token_missing` block
  (around line 339) with logic that:
  - If `result.id_token`: call `decode_id_token(result.id_token)` as today.
  - Else if `result.account_uuid` and `result.account_email`: construct a
    `ClaudeOauthClaims` directly:
    ```python
    claims = ClaudeOauthClaims(
        claude_account_uuid=result.account_uuid,
        user_email=result.account_email,
        user_organization_uuid=result.organization_uuid,
        scopes=result.scope.split() if result.scope else None,
    )
    ```
  - Else: raise `id_token_missing` as today (the genuine "no identity
    payload" case).
- Move `result.raw_body` logging to the `id_token_missing` branch only —
  the success path doesn't need it.

**Acceptance:** Anthropic's actual response shape
(`{"account": {...}, "organization": {...}}` with no `id_token`) produces a
`ClaudeOauthClaims` with `claude_account_uuid` set to the account's UUID.

## 5. Tests for the Anthropic actual-shape path

**Files:**
- `tests/unit/test_anthropic_oauth_exchange.py`
- `tests/unit/test_claude_oauth_service.py`

Add coverage for:

- **Exchange:** `_parse_exchange_success_body` populates `account_uuid`,
  `account_email`, `organization_uuid`, `organization_name` from a body
  shaped like Anthropic's actual response (use the captured real body from
  the 2026-07-15 incident; redact the literal access/refresh tokens but
  keep the structure).
- **Exchange:** `_parse_exchange_success_body` leaves the new fields `None`
  when the body has no `account`/`organization` keys (backward compat).
- **Exchange:** `_parse_exchange_success_body` tolerates `account` or
  `organization` being `None`/non-dict.
- **Service:** `complete_oauth` with a `code#state` paste and a body that
  returns account shape successfully persists the account row.
- **Service:** `complete_oauth` with `code#state` whose state half does not
  match `flow.state_token` raises `state_mismatch` (HTTP 400).
- **Service:** `complete_oauth` with a plain `code` (no `#`) and the
  account-shape body succeeds.
- **Service:** `complete_oauth` with no `id_token` AND no
  `account.{uuid, email_address}` raises `id_token_missing` (current
  behavior preserved for the genuine-missing case).

**Acceptance:** all new tests pass; the full pre-existing
`tests/unit/test_claude_oauth_*.py` and
`tests/unit/test_anthropic_oauth_*.py` suites pass.

## 6. Diagnostic logging (single, narrow)

**File:** `app/modules/claude/oauth/service.py`

Replace the previously ad-hoc in-container diagnostic block with a single
`logger.warning("claude.oauth.flow.callback.diagnostic", extra={...})` at
the start of `complete_oauth` covering only: flow_id, code length, code
head/tail, submitted_state_prefix, flow_state_prefix, states_match.
Replace the in-container `logger.error("claude.oauth.flow.id_token_missing",
extra={...})` with a version that uses `extra={}` (json log formatter
already preserves these in production).

Drop the in-container patches' `verifier_head`, `verifier_sha256_chal_pref`,
and the parsed_code_state log line — they were one-off diagnostics and
are not needed for the production fix.

**Acceptance:** `pytest tests/unit/test_claude_oauth_service.py -k diagnostic`
passes; no stray `verifier_head`/`verifier_sha256_chal_pref` /
`parsed_code_state` strings remain in the source tree.

## 7. OpenSpec delta spec

**File:** `openspec/changes/fix-claude-oauth-account-claims/specs/claude-oauth-pool/spec.md`

Add two `MODIFIED Requirements` (mirroring `add-claude-oauth-pool`):

1. **Claude account add via OAuth** — extend the existing requirement to
   accept either `id_token` or `account.{uuid, email_address}` +
   `organization.uuid` as the source of account identity, and to parse the
   `code#state` paste format.
2. **Token-exchange response handling** (new) — explicit requirement that
   the OAuth client extracts `account.*` / `organization.*` fields from
   the token response in addition to `id_token`.

Add one `ADDED Requirement`:

3. **`code#state` paste acceptance** (new) — the `POST /api/claude/oauth/callback`
   handler MUST accept the `code#state` format Anthropic's OOB page emits,
   split on `#`, validate the state half against the flow's stored
   `state_token`, and use only the code half for the exchange.

**Acceptance:** `openspec validate --specs --strict` clean.

## 8. Rollback the in-container patches

**Container:** `codex-lb-server-1` on `claude-test.bezproblem.vip`.

- Already done (2026-07-15 ~13:15 UTC) per the user's C option:
  `/app/app/modules/claude/oauth/service.py` restored from
  `/tmp/service.py.bak.1784105569` (pre-v1 backup), container restarted,
  PR #26 state-passing fix verified intact.
- Confirm rollback is still in place after the PR-merge + image-rebuild
  flow completes (i.e., the deployment serves the proper OpenSpec-backed
  code, not the ad-hoc container patches).

## 9. PR + auto-deploy verification

- Open PR from `fix/claude-oauth-account-claims` to `main`.
- Wait for CI green (Required checks per `.github/CONTRIBUTING.md`).
- Auto-deploy (per commit `655cfd6b feat(deploy): auto-deploy to test
  server on push to main (#4)`) rebuilds the image and redeploys to
  `claude-test.bezproblem.vip`.
- Operator retries the Add Claude account via OAuth flow end-to-end.
- Confirm Anthropic account row appears in `accounts` table with the
  `claude_account_uuid` matching the one returned in the OOB response
  (visible in `claude.oauth.flow.id_token_missing` diagnostic — will
  now log success path).