# claude-oauth-pool Specification (delta)

This delta codifies a previously-implicit invariant of the `claude-oauth-pool`
capability: OAuth flow state MUST persist across HTTP requests within a single
process. The delta also closes the production bug where the
`ClaudeOAuthService` flow store was instantiated per-request, causing every
Start → Submit pair in the dashboard to fail with `error_code: flow_not_found`.

## ADDED Requirements

### Requirement: OAuth flow state persists across HTTP requests within a process

The system SHALL make in-flight OAuth flow state (the `_FlowStore` keyed by
`flow_id` and `state_token`) accessible to every HTTP request that targets
the OAuth endpoints within the same Python process. In particular:

- The flow store SHALL be constructed exactly once per process during the
  FastAPI application lifespan and stored on `app.state.claude_oauth_flow_store`,
  mirroring the existing `app.state.claude_oauth_client` lifetime.
- The dependency `app.modules.claude.oauth.api.get_claude_oauth_service`
  SHALL read the flow store from `request.app.state.claude_oauth_flow_store`
  and SHALL pass it to `ClaudeOAuthService(flow_store=...)`. The fallback
  `flow_store or _FlowStore()` inside `ClaudeOAuthService.__init__` is
  preserved for unit tests that build the service in isolation.
- After `POST /api/claude/oauth/start` returns a `flow_id` to the client,
  the subsequent `POST /api/claude/oauth/callback` for that same `flow_id`
  — issued from the same dashboard session and routed to any HTTP handler
  in the same process — SHALL resolve the flow. A 404
  `error_code: flow_not_found` response in this scenario is a
  non-conformant regression of this requirement.

Multi-replica deployments remain out of scope: cross-replica flow state
continues to require sticky-session routing or a shared store. The
requirement applies only within a single process.

#### Scenario: Start then callback resolves the same flow

- **WHEN** an admin calls `POST /api/claude/oauth/start` and receives a `flow_id`
- **AND THEN** immediately calls `POST /api/claude/oauth/callback` with that same `flow_id` and a valid pasted `code` + matching `state`
- **THEN** the system returns 200 with `status: success` and the new account payload
- **AND** the call does NOT return 404 `error_code: flow_not_found`

#### Scenario: Status endpoint resolves the same flow

- **WHEN** an admin calls `POST /api/claude/oauth/start` and receives a `flow_id`
- **AND THEN** calls `GET /api/claude/oauth/status?flowId={id}` from the same process
- **THEN** the system returns 200 with `status: "pending"` (not `error: flow_not_found`)

#### Scenario: Cross-replica flow lookup is not guaranteed

- **GIVEN** the system is deployed with multiple replicas
- **WHEN** an admin starts the flow on replica A and submits the callback to replica B
- **THEN** the system MAY return 404 `error_code: flow_not_found`
- **AND** the operator-facing recovery SHALL be to start a new flow on the same replica (sticky-session routing) or for the deployment to use a shared flow store

### Requirement: Claude OAuth lifespan wiring exposes both client and flow store

The FastAPI application lifespan SHALL construct and expose the two
process-singleton OAuth collaborators on `app.state`:

- `claude_oauth_client` — the `ClaudeOAuthClient` wrapping the shared
  HTTP transport (existing requirement, unchanged).
- `claude_oauth_flow_store` — the `_FlowStore` instance used by
  `ClaudeOAuthService` to persist flow state across requests.

A missing `claude_oauth_flow_store` on `app.state` is a wiring bug. The
DI dependency SHALL raise `RuntimeError` with a message that names the
lifespan wiring step that owns this collaborator.

#### Scenario: Lifespan installs both collaborators

- **WHEN** the FastAPI application enters its lifespan context
- **THEN** `app.state.claude_oauth_client` is set to a `ClaudeOAuthClient`
- **AND** `app.state.claude_oauth_flow_store` is set to a `_FlowStore`

#### Scenario: Missing flow store fails fast

- **WHEN** the FastAPI application is started without the lifespan
  installing `app.state.claude_oauth_flow_store`
- **THEN** calling `POST /api/claude/oauth/start` or `/callback` returns 500
  with a `RuntimeError` whose message names `claude_oauth_flow_store` and
  points to `app.main::app_lifespan`