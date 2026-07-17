# feat-api-key-provider-scope-ui

## Why

`api_keys.provider_scope` was added in `add-claude-oauth-pool` together with a
backend `api_key_validator_with_provider('claude')` guard that rejects
codex-only keys against `POST /claude/v1/messages` (HTTP 403
`provider_scope_mismatch`). The DB column exists, the Pydantic schema accepts
the field, and the proxy enforces it — but the dashboard UI never exposes a
control to pick a provider, so every key created through the dialog gets the
backend default `["codex"]`. The result: operators cannot create a key that
authorizes Claude traffic without sending `POST /api/api-keys/` by hand via
curl with `{"providerScope": ["claude"]}`.

This change closes the UI gap without altering the backend contract.

## What

Add a single-choice radio control to the **Create API key** dialog so the
operator must pick exactly one of `codex` or `claude` before submitting.
Render the chosen scope in the **Edit API key** dialog as a read-only badge
— scope is immutable after creation.

The backend continues to accept arbitrary arrays (including `["codex",
"claude"]`) for API/curl callers; the UI policy is a UX constraint, not an
API policy.

## Impact

- Frontend only.
- No DB migration. No backend code changes. No backfill of existing rows.
- New i18n keys in `en.json` and `zh-CN.json`.
- New tests for both dialogs.
- New OpenSpec delta spec (this folder) syncs into `specs/api-keys/spec.md`
  via `/opsx:sync`.

## Out of scope

- Changing the backend `_validate_provider_scope` to reject `["codex","claude"]`
  (would break `tests/integration/test_api_keys_provider_scope.py::test_create_api_key_with_multiple_providers_returns_sorted_list`
  and the existing spec scenario in `add-claude-oauth-pool`).
- Letting the operator change `provider_scope` through the edit dialog.
- Adding new providers beyond `codex` / `claude`.
- Modifying the `add-claude-oauth-pool` change folder — it is still an open
  multi-concern change and adding UI work to it would conflate concerns.

## Failure modes

- **Submit without provider selected:** Zod `length(1)` validator on
  `providerScope` in the form schema raises a field-level `FormMessage`
  error and `onSubmit` is not called.
- **Edit dialog tries to mutate scope:** the edit dialog does not include
  `providerScope` in its PATCH payload, so the backend keeps the existing
  value. No backend behavior change.
- **Existing key with unexpected array shape (e.g. legacy `["codex","claude"]`
  created via curl):** the read-only badge falls back to "Codex" if the array
  is empty or contains only unknown values.