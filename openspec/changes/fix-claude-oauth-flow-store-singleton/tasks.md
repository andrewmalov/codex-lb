# Tasks

## 1. Spec delta

- [ ] 1.1 Add `### Requirement: Flow state persists across HTTP requests` to `specs/claude-oauth-pool/spec.md` with SHALL/MUST language and three Scenarios (single-process happy path, app-restart-must-not-inherit, multi-replica-out-of-scope).

## 2. Backend wiring (TDD)

- [ ] 2.1 New `app/modules/claude/wiring.py::build_claude_oauth_flow_store()` — returns a fresh `_FlowStore` (no async setup, just `return _FlowStore()`). Document that the caller MUST treat it as a process singleton.
- [ ] 2.2 `app/main.py::app_lifespan` — after `app.state.claude_oauth_client = build_claude_oauth_client()`, add `app.state.claude_oauth_flow_store = build_claude_oauth_flow_store()`. Mirror the existing client wiring.

## 3. API DI seam (TDD)

- [ ] 3.1 `app/modules/claude/oauth/api.py::get_claude_oauth_service` — read `flow_store = getattr(request.app.state, "claude_oauth_flow_store", None)`. Pass it as `flow_store=flow_store` into `ClaudeOAuthService(...)`.
- [ ] 3.2 Keep the `flow_store or _FlowStore()` fallback in `ClaudeOAuthService.__init__` so unit tests that build the service in isolation still work.

## 4. Tests

- [ ] 4.1 `tests/integration/test_claude_oauth_flow.py::stubbed_oauth_transport` — drop the manual `_FlowStore` workaround. The fixture still overrides `get_claude_oauth_service` for the transport stub, but reads `flow_store` from `request.app.state.claude_oauth_flow_store`.
- [ ] 4.2 `tests/integration/test_claude_oauth_errors.py::make_stubbed_oauth` — same cleanup as 4.1.
- [ ] 4.3 New `tests/integration/test_claude_oauth_flow_store_persists.py::test_start_then_callback_resolves_flow_via_real_di` — uses the **real** `get_claude_oauth_service` (no override). Stubs only the OAuth transport by replacing `app_instance.state.claude_oauth_client` with a `ClaudeOAuthClient(transport=stub, …)`. Asserts Start returns 200 with a flowId, Submit returns 200 and the account is visible in `GET /api/claude/accounts`. Without the fix, this test fails with `error_code=flow_not_found`.

## 5. Verify

- [ ] 5.1 `uv run pytest tests/integration/test_claude_oauth_flow.py tests/integration/test_claude_oauth_errors.py tests/integration/test_claude_oauth_flow_store_persists.py -v`
- [ ] 5.2 `uv run pytest tests/unit/test_claude_oauth_service.py -v`
- [ ] 5.3 `uv run ruff check .`
- [ ] 5.4 `uv run ty check app/modules/claude app/main.py`
- [ ] 5.5 Manual OpenSpec validation: read the delta `specs/claude-oauth-pool/spec.md` against the existing capability spec; confirm SHALL/SHALL NOT language is consistent with the surrounding requirements.