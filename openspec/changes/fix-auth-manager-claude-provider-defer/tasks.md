# Tasks

## 1. Test (RED) — `tests/unit/test_auth_manager.py`

Add a test that captures the 2026-07-17 incident:

```python
async def test_refresh_account_defers_for_claude_provider(monkeypatch):
    """Regression: Codex AuthManager must NOT rotate Claude rows.

    A Claude row's Codex-flavored `refresh_token_encrypted` column holds
    the literal placeholder "claude" (encrypted). Sending that to the
    Codex OAuth endpoint returns 400 invalid_grant → permanent failure →
    `update_status(account.id, REAUTH_REQUIRED, ...)`. Claude rotation
    is owned by `app.core.auth.guardian.AuthGuardianScheduler`; the Codex
    AuthManager must short-circuit for Claude rows.
    """
    refresh_called = False

    async def _fake_refresh(*args, **kwargs):
        nonlocal refresh_called
        refresh_called = True
        raise AssertionError("must not be called")

    monkeypatch.setattr(auth_manager_module, "refresh_access_token", _fake_refresh)

    encryptor = TokenEncryptor()
    account = Account(
        id="claude-491c2857-30eb-49ce-ad07-2b601efa041d",
        email="user@example.test",
        plan_type="claude_subscription",
        provider="claude",
        chatgpt_account_id=None,
        claude_account_uuid="491c2857-30eb-49ce-ad07-2b601efa041d",
        claude_user_email="user@example.test",
        claude_user_organization_uuid=None,
        access_token_encrypted=encryptor.encrypt("claude"),         # placeholder
        refresh_token_encrypted=encryptor.encrypt("claude"),        # placeholder
        id_token_encrypted=encryptor.encrypt("claude"),             # placeholder
        claude_access_token_encrypted=encryptor.encrypt("real-claude-access"),
        claude_refresh_token_encrypted=encryptor.encrypt("real-claude-refresh"),
        last_refresh=datetime(2026, 1, 1),
        status=AccountStatus.ACTIVE,
    )

    repo = _DummyRepo()
    manager = AuthManager(cast(AccountsRepositoryPort, repo))
    result = await manager.refresh_account(account)

    assert result is account
    assert result.status == AccountStatus.ACTIVE
    assert repo.status_payload is None
    assert repo.tokens_payload is None
    assert refresh_called is False
```

Run: `uv run pytest tests/unit/test_auth_manager.py::test_refresh_account_defers_for_claude_provider -q`

Confirm RED — the test fails because `refresh_account` calls
`refresh_access_token` for Claude rows.

## 2. Fix (GREEN) — `app/modules/accounts/auth_manager.py`

Add a provider check at the top of `refresh_account`:

```python
async def refresh_account(self, account: Account) -> Account:
    if getattr(account, "provider", None) == "claude":
        # Claude OAuth rotation is owned by AuthGuardianScheduler.
        # The Codex AuthManager must not attempt to rotate Claude
        # rows: the Codex-flavored refresh_token column holds the
        # literal placeholder "claude", which the Codex OAuth endpoint
        # rejects with 400 invalid_grant — which the existing failure
        # branch would surface as a REAUTH_REQUIRED flip. Mirror the
        # rationale from PR #30's _ClaudeAuthManagerAdapter (see
        # openspec/changes/fix-model-refresh-scheduler-provider-scope).
        return account
    refresh_token = self._encryptor.decrypt(account.refresh_token_encrypted)
    ...
```

Run: `uv run pytest tests/unit/test_auth_manager.py::test_refresh_account_defers_for_claude_provider -q`

Confirm GREEN.

## 3. Regression — `tests/unit/test_auth_manager.py` + `test_model_refresh_scheduler.py`

- Run the full `test_auth_manager.py` suite: must stay green (no
  Codex-account regression).
- Run the `test_model_refresh_scheduler.py` suite: the existing
  scheduler regression tests use a fixture for Claude accounts; they
  should stay green because `_ClaudeAuthManagerAdapter` is unchanged.

## 4. Full local gates

```bash
uvx ruff check app tests
uv run ty check app
uv run pytest tests/unit -q
openspec validate --strict fix-auth-manager-claude-provider-defer
```

All four must pass.

## 5. Deploy + verify

- Push branch → auto-deploy to `claude-test.bezproblem.vip`.
- From the dashboard: re-add a Claude account via OAuth-link.
- Within ~5 s the dashboard calls `/usage-reset-credits`. With the fix
  the account stays `status='active'`; the
  `AccountUsageResetCreditsUnavailableError` 409 may still be raised
  because Claude has no `/v1/usage` endpoint to query, but it must NOT
  flip the account status. Verify in `docker logs` that
  `Token refresh failed status=401` is NOT logged for the Claude
  account, and that the scheduler's
  `Model registry refresh produced no results` warn disappears once
  the access token is fresh.
