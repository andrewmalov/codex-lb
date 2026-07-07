# add-claude-oauth-link

Add OAuth-based add flow for Claude accounts. Companion change to
[`add-claude-oauth-pool`](../add-claude-oauth-pool/), which introduced the manual
paste path; this change delivers the authorization_code + PKCE path that the original
proposal deferred.

## What it does

Operators can click **Add Claude account via OAuth** in the dashboard, get a generated
authorization URL, complete consent on `claude.ai`, paste the resulting code back into
the dialog, and have codex-lb do the authorization-code exchange and persist the new
account through the same encryption + insert path used by the manual paste endpoint.

No local HTTP callback server, no port-forwarding, no docker-exposed port. Works
identically in localhost, Docker, and headless setups.

## TL;DR

- New module: `app/modules/claude/oauth/` (isolated from `app/modules/oauth/`).
- New endpoints: `POST /api/claude/oauth/start`, `GET /api/claude/oauth/status`,
  `POST /api/claude/oauth/callback`.
- New client method: `ClaudeOAuthClient.exchange_authorization_code(...)`.
- New auth manager method: `ClaudeAuthManager.add_claude_account_from_oauth(...)`.
- New frontend dialog: `AddClaudeAccountOAuthDialog`.
- Existing `POST /api/claude/accounts` (manual paste) unchanged.

## Artifacts

| File                                          | Purpose                                                  |
|-----------------------------------------------|----------------------------------------------------------|
| `proposal.md`                                 | Why + what + impact                                      |
| `tasks.md`                                    | Implementation checklist                                 |
| `design.md`                                   | Full design (architecture, state machine, API contract)  |
| `specs/claude-oauth-pool/spec.md`             | Normative delta spec (ADDED Requirements + Scenarios)    |
| `context.md`                                  | Operational notes, assumptions, failure modes            |

## Validation

```bash
openspec validate add-claude-oauth-link --strict --no-interactive
```

## Status

Active. Not yet implemented.
