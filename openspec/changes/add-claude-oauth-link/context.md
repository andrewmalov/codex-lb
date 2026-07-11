# Context — Claude account add via OAuth

Operational context for change `add-claude-oauth-link`. The normative spec lives at
`specs/claude-oauth-pool/spec.md`; this file holds rationale, operational notes, and
known limitations.

## Why this change exists

`add-claude-oauth-pool` (shipped) introduced `POST /api/claude/accounts` (manual paste)
and explicitly deferred PKCE-based authorization code flow to a follow-up. Without that
follow-up, operators who want to add a Claude account have to:

1. Run `claude code` locally to obtain tokens, OR
2. Use a third-party tool to extract tokens from a Claude Code session, OR
3. Hand-craft the OAuth dance and type every field into the dashboard.

Each path is friction and each provides an opportunity to mistype a token. The OAuth
add flow brings Claude to parity with how the Codex provider already accepts accounts
(browser/device/manual callback in `app/modules/oauth/`).

## Why no local callback server

A local HTTP callback server on a fixed port (Codex uses `1455`) was considered and
explicitly rejected:

- **Port conflicts.** If both Claude and Codex flows were live, only one can own 1455.
- **Remote access.** Browsers cannot reach `localhost:1455` on the operator's machine
  when the dashboard runs in Docker or behind a reverse proxy. The codex-flow solves
  this with a "manual callback" that requires the user to copy the full redirect URL.
- **Claude Code's own flow.** The Anthropic-side OOB-style redirect
  (`https://console.anthropic.com/oauth/code`) is exactly what Claude Code itself uses.
  Replicating that flow keeps the user on rails they already know.

The trade-off is that the user has to copy a short code from a rendered page back into
the dialog instead of having the redirect happen transparently. We judge that acceptable
because it works in every deployment shape without port-forwarding.

## Redirect URI verification

`https://console.anthropic.com/oauth/code` is verified against multiple independent
sources documenting the Claude Code OAuth behavior:

- Claude Code source reverse-engineering writeups that show the same redirect URI used
  by Anthropic's official Claude Code CLI for its OOB-style code display.
- AWS-published Claude Platform documentation that describes the OOB pattern for
  non-browser-mediated authorization.

If the redirect URI changes (Anthropic does not publish semver for OAuth endpoints),
operators override `CODEX_LB_CLAUDE_OAUTH_REDIRECT_URI` without code changes.

## `id_token` claim mapping — assumptions

Anthropic does not publish a documented `id_token` schema. The fallback chain in
`app/modules/claude/oauth/tokens.py` was assembled from multiple captures of the
public Claude Code OAuth handshake:

| Account field                 | Claim candidates (first match wins)                                            |
|-------------------------------|--------------------------------------------------------------------------------|
| `claude_account_uuid`         | `account_id`, `sub` (UUID-shaped), `https://api.anthropic.com/account_id`      |
| `user_email`                  | `email`, `https://api.anthropic.com/email`                                     |
| `user_organization_uuid`      | `organization_id`, `org_id`, `https://api.anthropic.com/organization_id`       |
| `scopes`                      | split of `scope` or `scp` by whitespace                                         |

If Anthropic's `id_token` shape diverges from these captures, the worst case is that
`add-claude-oauth-link` returns 400 `id_token_claims_incomplete` with a pointer to the
manual paste endpoint. Manual paste continues to work because it accepts every field
directly from the operator.

## Signature verification

`id_token` is decoded without signature verification. This matches the project's
existing convention (see `app/core/auth/models.py::extract_id_token_claims`) and is
defensible because the only consumer of the claims is the local account-creation path;
the proxy does not authorize third parties based on the `id_token`. If signature
verification becomes necessary, it can be added as a separate change that introduces
a JWKS client; that change is explicitly out of scope here.

## Single-in-flight semantics

Only one non-terminal Claude OAuth flow can exist at a time. New `/start` requests
supersede any pending flow. Rationale:

- A second flow without supersession would create two parallel PKCE verifiers in
  memory and two parallel authorization URLs in circulation; both would be valid, and
  the operator would not know which URL is the canonical one.
- Operators who add multiple accounts in parallel are a niche; sequencing is acceptable.
- The dialog disables the primary "Add" button while a flow is pending, preventing
  accidental supersession.

## Multi-replica caveat

The state store is process-local. Behavior in a multi-replica deployment:

- If `/start` and `/callback` land on different replicas, the callback returns
  404 `flow_not_found`.
- The Codex OAuth flow has the same caveat (see `app/modules/oauth/service.py`).
- Operators using the Helm chart with > 1 replica must use sticky-session routing
  on the dashboard hostnames OR issue `/start` and `/callback` quickly enough that the
  load balancer's session affinity keeps them on the same pod.

A follow-up change can introduce a shared state store (Redis or DB-backed). It is
explicitly out of scope here because it expands the change into ops surface beyond
"add a new account".

## Operational failure modes

| Symptom | Most likely cause | Operator action |
|---------|--------------------|-----------------|
| `/start` returns 502 | `claude_oauth_authorize_endpoint` unreachable | Check `claude_oauth_extra_headers` and outbound connectivity |
| Callback returns 502 `invalid_grant` | Code pasted more than once, or expired | Click "Start over", redo the consent |
| Callback returns 400 `id_token_missing` | Anthropic did not return `id_token` (rare for Claude Code subs) | Switch to manual paste for that account |
| Callback returns 400 `state_mismatch` | Operator typed a wrong state token | Click "Start over" |
| Account shows up but immediately fails subsequent requests | Account row has bad credentials from upstream | Disable + delete + retry; auth guardian will log `claude.refresh.failed` |
| `/callback` returns 404 on a multi-replica cluster | Flow started on a different pod | Re-deploy with sticky sessions, or do `/start` + `/callback` in quick succession on the same session |

## What this change does NOT do

- Does not modify `app/modules/oauth/*` (Codex flow).
- Does not modify `app/modules/proxy/service.py` (ADR-0001).
- Does not modify `POST /api/claude/accounts` (manual paste stays for compatibility).
- Does not add a database migration (the existing schema from `add-claude-oauth-pool`
  is reused as-is).
- Does not change rate-limit handling, refresh behavior, or proxy routing.
