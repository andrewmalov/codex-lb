# Context — fix-claude-oauth-account-claims

## Live trace that proved this is needed

Captured 2026-07-15 on `claude-test.bezproblem.vip` (single replica,
image `ghcr.io/andrewmalov/codex-lb:main`, log driver `json-file`,
container id `3f7b90a8f7cbce201773209e10ba813cc6d4bfe27fd0f74f9854d019c73e363e`).

The diagnostic patch we ran in-container was rolled back to PR #26
baseline as part of the user's option C (proper OpenSpec change). The
captured raw_body from the final failing exchange proves the bug:

```json
{
  "token_type": "Bearer",
  "access_token": "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",
  "expires_in": 28800,
  "refresh_token": "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",
  "scope": "user:inference user:profile",
  "token_uuid": "7f7a49a7-dd42-4f17-96fc-d8f115cd68f5",
  "refresh_token_expires_in": 2502728,
  "organization": {
    "uuid": "cb355b7e-1b37-441c-8e2f-6f230a65a773",
    "name": "kusanat5@gmail.com's Organization"
  },
  "account": {
    "uuid": "491c2857-30eb-49ce-ad07-2b601efa041d",
    "email_address": "kusanat5@gmail.com"
  }
}
```

No `id_token` field. Account identity lives in `account.uuid` /
`account.email_address`. Codex-lb's spec-required `decode_id_token` path
fails with `id_token_claims_incomplete` (or in the just-previous incident,
`id_token_missing`).

The `code#state` paste format was proven by:

```text
2026-07-15T12:25:40Z claude.oauth.flow.callback.parsed_code_state code_len_after_split=48
2026-07-15T12:25:40Z claude.oauth.flow.callback.diagnostic flow_id=tILCE1NcnPMlzxMs
  age_s=20.859 verifier_len=86 verifier_head=Dde19C4z verifier_sha256_chal_pref=ANQHeFN0
  state_prefix=IIoyR2Ue code_len=48 code_head=YI1ubtmJ code_tail=o6T3fp
  submitted_state_prefix=IIoyR2Ue states_match=True redirect_uri=...
```

`code_len=48` after the `code#state` split — the bare code Anthropic
issued. The 92-character input the operator pasted
(`P1isG7Yw...#<state>`) parses cleanly into a 48-char code and a
matching state.

## Reference: how Claude Code CLI and `querymt/anthropic-auth` do it

`querymt/anthropic-auth` is the closest reference (Rust library
implementing exactly this Anthropic OAuth flow). Their
`parse_code_and_state` (in `src/client/blocking.rs`):

```rust
fn parse_code_and_state(code_with_state: &str, expected_state: &str) -> Result<(String, String)> {
    if let Some(hash_pos) = code_with_state.find('#') {
        let code = &code_with_state[..hash_pos];
        let returned_state = &code_with_state[hash_pos + 1..];
        if returned_state != expected_state {
            return Err(/* state mismatch */);
        }
        Ok((code.to_string(), returned_state.to_string()))
    } else {
        Ok((code_with_state.to_string(), expected_state.to_string()))
    }
}
```

This change ports that parser to `ClaudeOAuthService.complete_oauth`.
The `id_token`-vs-account-claims branch is the new part — `querymt/anthropic-auth`
doesn't surface identity to the caller (it returns only the tokens), so
the caller has to call Anthropic's `/api/oauth/profile` separately. We
keep identity extraction server-side because codex-lb's `ClaudeAccount`
schema needs `claude_account_uuid` (UUID) and `user_email` as required
fields at insert time.

## Why the rollback is "safe"

The diagnostic patch we ran in-container (`v1`-`v5`) was rolled back to
the pre-v1 state captured in `/tmp/service.py.bak.1784105569`. The PR #26
state-passing fix is preserved (`state=flow.state_token` and `state: str`
both present). The container is running the same code that was on
production before the diagnostic, plus PR #26's correctness fix.

## Why this isn't a `paper-verification-trap` regression

The claim "Anthropic returns account identity in `account.*` / `organization.*`
for the public client `9d1c250a-...` and not as an OIDC id_token" was
verified against a **live token-exchange response** captured in
container logs, not against documentation alone. `querymt/anthropic-auth`
exists and is the same flow shape, so cross-checking was trivial.

The `code#state` paste convention was verified against:
1. The Claude Code CLI's own output (`~/.claude/.credentials.json` was
   always built from this flow).
2. `querymt/anthropic-auth`'s `parse_code_and_state` (literal #1
   splitting logic, validated).
3. Anthropic's OOB page DOM (`page-9fe5d18e75eb9340.js` JS chunk
   reads `code` and `state` separately from URL params — confirming the
   `#`-joined display is a presentation choice, not a wire format).

## Multi-replica caveat (unchanged)

`add-claude-oauth-link/context.md §Multi-replica semantics` already notes
that `_FlowStore` is process-local. This change preserves that. A
multi-replica deployment without sticky-session routing would still fail
account-add on the wrong replica → `flow_not_found`. Not in scope here.

## Related
- `openspec/changes/diagnose-claude-oauth-add-blocker/` — full server
  transcript + settings dump from the 2026-07-15 incident.
- `memory/anthropic-oauth-claude-code-redirect-uri` — URL shape
  Anthropic accepts for client `9d1c250a-...`.
- `memory/paper-verification-trap` — applies to anyone proposing
  endpoint/recipe changes without live verification.
- `memory/claude-test-deployment` — deployment shape of
  `claude-test.bezproblem.vip` (single replica, `:main` image,
  bind-mount SQLite).