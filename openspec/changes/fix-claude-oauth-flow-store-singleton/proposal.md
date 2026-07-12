## Why

The Claude OAuth link flow (shipped in PR #14 / `#add-claude-oauth-link`) has a
latent bug that surfaces as `error_code=flow_not_found` whenever the operator
finishes the callback in a different HTTP request than the one that started the
flow — which is *always*, because Start and Submit are necessarily two separate
HTTP requests.

`app/modules/claude/oauth/api.py::get_claude_oauth_service` is a FastAPI
dependency that is invoked **on every HTTP request**. Each call constructs a
fresh `ClaudeOAuthService`. `ClaudeOAuthService.__init__` initialises
`self._store = flow_store or _FlowStore()`, so when `flow_store` is not
provided (the production code path), each request gets its **own empty**
`_FlowStore`. The flow created in the Start request lives in Store A; the
Submit request creates Store B (empty), looks up the flow id, and raises
`ClaudeOauthFlowError("flow_not_found", ..., http_status=404)`. The dashboard
renders this as `claude.oauth.error.flow_not_found` —
"Authorization request not found. Please start over."

The bug reproduces **on a single-replica, single-worker deployment** — it is
not a multi-replica problem. It is also not a TTL / expiry problem: even a
zero-second turnaround from Start to Submit triggers it. The only way the
existing flow happened to work in tests is that `tests/integration/test_claude_oauth_flow.py`
explicitly works around it with a `dependency_overrides` fixture that manually
shares one `_FlowStore` across every request (see lines 95-96 and 128-150 of
that file). Operators running the dashboard hit the bug 100% of the time.

PR #16 corrected the OAuth authorize endpoint defaults and the `code=true`
flag — that fix landed separately and is unrelated. This change addresses the
flow-store singleton bug.

## What Changes

- **Backend wiring**: `app/modules/claude/wiring.py` exposes
  `build_claude_oauth_flow_store()` that constructs the singleton
  `_FlowStore` instance.
- **Lifespan**: `app/main.py::app_lifespan` stores the singleton on
  `app.state.claude_oauth_flow_store` alongside the existing
  `claude_oauth_client`.
- **API DI**: `app/modules/claude/oauth/api.py::get_claude_oauth_service`
  reads `flow_store` from `request.app.state.claude_oauth_flow_store` and
  passes it to `ClaudeOAuthService(...)`. The fallback in
  `ClaudeOAuthService.__init__` (`flow_store or _FlowStore()`) is preserved
  so unit tests that build the service in isolation continue to work.
- **Test cleanup**: `tests/integration/test_claude_oauth_flow.py` and
  `tests/integration/test_claude_oauth_errors.py` drop the manual
  `_FlowStore` workaround. Their fixtures still override the transport
  seam (Anthropic is real in tests must be stubbed), but the flow store
  now comes from `app.state.claude_oauth_flow_store` — the production
  source of truth.
- **Regression test**: New `tests/integration/test_claude_oauth_flow_store_persists.py`
  exercises Start → Submit through the real `get_claude_oauth_service`
  (no dependency override) and asserts that the callback resolves the
  flow. Without this fix the test fails with `error_code=flow_not_found`;
  with the fix it succeeds.
- **Spec delta**: `specs/claude-oauth-pool/spec.md` gains a
  `### Requirement: Flow state persists across HTTP requests` requirement
  with SHALL/MUST language, plus three Scenarios covering the
  single-process / multi-process / cross-replica paths.

## Capabilities

### Modified Capabilities

- `claude-oauth-pool`: codify that flow state MUST persist across HTTP
  requests within a single process and SHALL be shared via
  `app.state` (mirroring the existing `claude_oauth_client` lifetime).
  Multi-replica caveats are documented in `context.md`.

## Impact

- **Backend**: 3 small edits in
  `app/modules/claude/wiring.py` + `app/main.py` +
  `app/modules/claude/oauth/api.py`. No new dependencies.
- **Tests**: existing two integration tests drop their workaround and
  one new integration test is added.
- **Operators**: zero-config fix — flow state now survives across HTTP
  requests automatically. No env-var changes, no behaviour change for
  flows that previously worked.
- **Multi-replica**: still requires sticky-session routing or a shared
  store to work across replicas. Documented in `context.md` as the
  pre-existing caveat from `add-claude-oauth-link`; out of scope here.