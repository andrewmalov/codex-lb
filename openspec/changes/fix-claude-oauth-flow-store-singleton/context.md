# Context — Fix Claude OAuth flow store singleton

Operational notes for `fix-claude-oauth-flow-store-singleton`. The normative
requirement lives at `specs/claude-oauth-pool/spec.md`.

## How the bug manifests

Operator flow in the dashboard:

1. Click "Add Claude account via OAuth" → dialog opens.
2. Click "Start" → `POST /api/claude/oauth/start` → backend builds flow in
   `ServiceA._store` (Store A), returns `{flowId, stateToken, authorizationUrl, …}`.
3. Open URL in new tab, authorize on `claude.ai`, copy code.
4. Paste code, click Submit → `POST /api/claude/oauth/callback` → FastAPI
   resolves `get_claude_oauth_service` again → builds `ServiceB._store`
   (Store B, **empty**).
5. `ServiceB.complete_oauth` → `self._store.get_by_id(flowId)` →
   `None` → `ClaudeOauthFlowError("flow_not_found", http_status=404)` →
   dashboard renders the i18n string
   `claude.oauth.error.flow_not_found` =
   "Authorization request not found. Please start over."

The error is **deterministic** — it happens on every single Start → Submit
sequence in production, with any combination of replicas / workers, because
each HTTP request constructs a new `ClaudeOAuthService`.

## Why the existing test suite did not catch it

`tests/integration/test_claude_oauth_flow.py` lines 95-96:

> *"The ``ClaudeOAuthService`` flow store is process-local state, so we share
> one ``_FlowStore`` instance across every request in the test."*

The integration test fixture manually constructs one `_FlowStore` and passes
it to every `ClaudeOAuthService` it builds via `dependency_overrides`. The
fixture exists *because the bug exists*. Without it the existing
`test_oauth_link_flow_creates_account_and_does_not_leak_tokens` test would
fail with `error_code=flow_not_found`.

After this fix:

- The fixture still overrides the transport seam (Anthropic is real in
  tests must be stubbed — see `_StubOAuthTransport`).
- The fixture no longer needs to override the `_FlowStore` lifetime —
  the real `app.state.claude_oauth_flow_store` is shared automatically.
- A new `test_claude_oauth_flow_store_persists` regression test asserts
  the property end-to-end through the real DI graph.

## Multi-replica caveat (out of scope)

The `_FlowStore` is process-local. A deployment with multiple replicas
(e.g. HPA scaling, blue/green, rolling restart) still requires either:

- **Sticky-session routing** at the load balancer (route Start and Submit
  for the same operator to the same replica), or
- **Shared state** (Redis / DB-backed store), or
- **Operator-side awareness** that opening the dialog twice opens two
  flows on potentially two replicas and one will fail.

This caveat was already documented in
`openspec/changes/add-claude-oauth-link/context.md`. The fix in this
change narrows the bug surface from "every flow" to "flows that cross
replica boundaries" — a strict improvement.

## Why we keep the `flow_store or _FlowStore()` fallback

`ClaudeOAuthService.__init__` still accepts a `flow_store=None` and falls
back to a fresh `_FlowStore()`. Unit tests construct the service in
isolation (`tests/unit/test_claude_oauth_service.py`) and rely on that
fallback to keep each test independent. Removing the fallback would
require refactoring every unit test; keeping it preserves the public
constructor contract and isolates the change to the production DI seam.

## Operator upgrade notes

- Existing operators with a single-replica, single-worker deployment
  get the fix automatically by upgrading the image. No config changes.
- Existing operators with multiple replicas: still need sticky-session
  routing. The change does not regress their behaviour.
- No env-var changes. No new settings.