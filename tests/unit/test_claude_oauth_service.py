"""Tests for ``app.modules.claude.oauth.service.ClaudeOAuthService``.

State-machine behavior, single-in-flight supersession, TTL expiry, CSRF
state validation, and the full Anthropic stub round-trip — every documented
``error_code`` is exercised at least once.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.core.clients.anthropic.errors import ClaudeAuthError, ClaudeUpstreamError
from app.core.clients.anthropic.oauth import (
    ClaudeAuthorizationCodeResult,
)
from app.modules.claude.auth_manager import ClaudeAccountAlreadyExists
from app.modules.claude.oauth import service as service_module
from app.modules.claude.oauth.service import ClaudeOAuthService

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


@dataclass
class _FakeOAuthClient:
    """Stub matching the surface that ``ClaudeOAuthService`` consumes."""

    next_result: ClaudeAuthorizationCodeResult | None = None
    next_error: Exception | None = None
    last_code: str | None = None
    last_code_verifier: str | None = None
    last_state: str | None = None
    last_redirect_uri: str | None = None

    async def exchange_authorization_code(
        self,
        *,
        code: str,
        code_verifier: str,
        state: str,
        redirect_uri: str,
    ) -> ClaudeAuthorizationCodeResult:
        self.last_code = code
        self.last_code_verifier = code_verifier
        self.last_state = state
        self.last_redirect_uri = redirect_uri
        if self.next_error is not None:
            raise self.next_error
        assert self.next_result is not None, "test must set either next_result or next_error"
        return self.next_result


@dataclass
class _FakeAuthManager:
    """Stub matching the surface ``ClaudeOAuthService`` uses."""

    next_account_id: str = "claude-uuid-X"
    next_error: Exception | None = None
    last_access_token: str | None = None
    last_refresh_token: str | None = None
    last_expires_in: int | None = None
    last_claims: Any = None

    async def add_claude_account_from_oauth(
        self,
        *,
        access_token: str,
        refresh_token: str,
        expires_in: int,
        id_token_claims: Any,
    ) -> str:
        self.last_access_token = access_token
        self.last_refresh_token = refresh_token
        self.last_expires_in = expires_in
        self.last_claims = id_token_claims
        if self.next_error is not None:
            raise self.next_error
        return self.next_account_id


def _make_settings(*, ttl: int = 600) -> Any:
    return SimpleNamespace(
        claude_oauth_authorize_endpoint="https://auth.example.test/oauth/authorize",
        claude_oauth_client_id="client-id-xyz",
        claude_oauth_redirect_uri="https://r.example.test/cb",
        claude_oauth_scopes="user:profile user:inference",
        claude_oauth_flow_ttl_seconds=ttl,
    )


def _make_service(
    *,
    client: _FakeOAuthClient | None = None,
    auth_manager: _FakeAuthManager | None = None,
    ttl: int = 600,
    settings: Any | None = None,
) -> tuple[ClaudeOAuthService, _FakeOAuthClient, _FakeAuthManager]:
    settings = settings or _make_settings(ttl=ttl)
    client = client or _FakeOAuthClient()
    auth_manager = auth_manager or _FakeAuthManager()
    svc = ClaudeOAuthService(
        settings=settings,
        oauth_client=client,  # type: ignore[arg-type]
        auth_manager=auth_manager,  # type: ignore[arg-type]
    )
    return svc, client, auth_manager


def _b64u(payload: str) -> str:
    import base64

    return base64.urlsafe_b64encode(payload.encode()).rstrip(b"=").decode("ascii")


def _id_token(payload: dict) -> str:
    import base64
    import json

    header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode("ascii")
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode("ascii")
    return f"{header}.{body}.fakesig"


# ---------------------------------------------------------------------------
# start_oauth
# ---------------------------------------------------------------------------


async def test_start_oauth_returns_authorization_url_with_pkce() -> None:
    svc, _, _ = _make_service()
    resp = await svc.start_oauth()

    assert resp.flow_id
    assert resp.state_token  # exposed to dashboard session
    assert resp.authorization_url.startswith("https://auth.example.test/oauth/authorize?")
    assert "code_challenge=" in resp.authorization_url
    assert "code_challenge_method=S256" in resp.authorization_url
    assert "state=" in resp.authorization_url
    assert resp.expires_in_seconds == 600
    assert resp.redirect_uri == "https://r.example.test/cb"
    assert resp.callback_instructions


async def test_start_oauth_supersedes_previous_pending_flow() -> None:
    svc, _, _ = _make_service()

    first = await svc.start_oauth()
    second = await svc.start_oauth()

    assert first.flow_id != second.flow_id

    status_first = await svc.oauth_status(first.flow_id)
    assert status_first.status == "error"
    assert status_first.error_code == "superseded"

    status_second = await svc.oauth_status(second.flow_id)
    assert status_second.status == "pending"


# ---------------------------------------------------------------------------
# oauth_status
# ---------------------------------------------------------------------------


async def test_oauth_status_unknown_flow_returns_not_found_code() -> None:
    svc, _, _ = _make_service()
    status = await svc.oauth_status("nonexistent")
    assert status.status == "error"
    assert status.error_code == "flow_not_found"


async def test_oauth_status_ttl_expired_marks_flow_error() -> None:
    svc, _, _ = _make_service(ttl=0)
    started = await svc.start_oauth()

    status = await svc.oauth_status(started.flow_id)
    assert status.status == "error"
    assert status.error_code == "flow_expired"


# ---------------------------------------------------------------------------
# complete_oauth
# ---------------------------------------------------------------------------


async def test_complete_oauth_happy_path_creates_account() -> None:
    svc, client, mgr = _make_service()
    started = await svc.start_oauth()
    client.next_result = ClaudeAuthorizationCodeResult(
        access_token="AT",
        refresh_token="RT",
        id_token=_id_token(
            {
                "account_id": "acct-1",
                "email": "u@example.test",
                "scope": "user:inference",
            }
        ),
        expires_in=3600,
        scope="user:inference",
    )

    resp = await svc.complete_oauth(
        flow_id=started.flow_id,
        code="AUTH_CODE",
        state=started.state_token,
    )

    assert resp.status == "success"
    assert resp.account.id == "claude-uuid-X"

    # PKCE verifier was passed to the client
    assert client.last_code == "AUTH_CODE"
    assert client.last_redirect_uri == "https://r.example.test/cb"
    assert client.last_code_verifier and len(client.last_code_verifier) >= 43
    assert client.last_state == started.state_token

    # Typed claims flowed into the auth manager
    assert mgr.last_access_token == "AT"
    assert mgr.last_refresh_token == "RT"
    assert mgr.last_expires_in == 3600
    assert mgr.last_claims.claude_account_uuid == "acct-1"

    # Status flips to success
    status = await svc.oauth_status(started.flow_id)
    assert status.status == "success"
    assert status.account_id == "claude-uuid-X"


async def test_complete_oauth_state_mismatch_returns_error_code() -> None:
    svc, _, _ = _make_service()
    started = await svc.start_oauth()

    with pytest.raises(service_module.ClaudeOauthFlowError) as exc:
        await svc.complete_oauth(
            flow_id=started.flow_id,
            code="AUTH_CODE",
            state="DIFFERENT_STATE",
        )
    assert exc.value.code == "state_mismatch"


async def test_complete_oauth_flow_not_found() -> None:
    svc, _, _ = _make_service()
    with pytest.raises(service_module.ClaudeOauthFlowError) as exc:
        await svc.complete_oauth(flow_id="nope", code="c", state="s")
    assert exc.value.code == "flow_not_found"


async def test_complete_oauth_invalid_grant_propagates_as_upstream_error() -> None:
    svc, client, _ = _make_service()
    started = await svc.start_oauth()
    client.next_error = ClaudeAuthError("invalid_grant: bad")

    with pytest.raises(service_module.ClaudeOauthFlowError) as exc:
        await svc.complete_oauth(flow_id=started.flow_id, code="C", state=started.state_token)
    assert exc.value.code == "invalid_grant"


async def test_complete_oauth_anthropic_5xx_propagates_as_unreachable() -> None:
    svc, client, _ = _make_service()
    started = await svc.start_oauth()
    client.next_error = ClaudeUpstreamError("upstream 503")

    with pytest.raises(service_module.ClaudeOauthFlowError) as exc:
        await svc.complete_oauth(flow_id=started.flow_id, code="C", state=started.state_token)
    assert exc.value.code == "anthropic_unreachable"


async def test_complete_oauth_account_already_exists_returns_409_error() -> None:
    svc, client, mgr = _make_service()
    started = await svc.start_oauth()
    client.next_result = ClaudeAuthorizationCodeResult(
        access_token="AT",
        refresh_token="RT",
        id_token=_id_token({"account_id": "dup"}),
        expires_in=3600,
        scope="x",
    )
    mgr.next_error = ClaudeAccountAlreadyExists("dup")

    with pytest.raises(service_module.ClaudeOauthFlowError) as exc:
        await svc.complete_oauth(flow_id=started.flow_id, code="C", state=started.state_token)
    assert exc.value.code == "account_already_exists"


async def test_complete_oauth_id_token_missing_returns_error_code() -> None:
    svc, client, _ = _make_service()
    started = await svc.start_oauth()
    client.next_result = ClaudeAuthorizationCodeResult(
        access_token="AT",
        refresh_token="RT",
        id_token=None,
        expires_in=3600,
        scope="x",
    )

    with pytest.raises(service_module.ClaudeOauthFlowError) as exc:
        await svc.complete_oauth(flow_id=started.flow_id, code="C", state=started.state_token)
    assert exc.value.code == "id_token_missing"


async def test_complete_oauth_id_token_claims_incomplete_returns_error_code() -> None:
    svc, client, _ = _make_service()
    started = await svc.start_oauth()
    # id_token present but no claude_account_uuid-derivable claim
    client.next_result = ClaudeAuthorizationCodeResult(
        access_token="AT",
        refresh_token="RT",
        id_token=_id_token({"email": "only@example.test"}),
        expires_in=3600,
        scope="x",
    )

    with pytest.raises(service_module.ClaudeOauthFlowError) as exc:
        await svc.complete_oauth(flow_id=started.flow_id, code="C", state=started.state_token)
    assert exc.value.code == "id_token_claims_incomplete"


async def test_complete_oauth_flow_already_terminal_returns_not_pending() -> None:
    svc, client, _ = _make_service()
    started = await svc.start_oauth()
    client.next_result = ClaudeAuthorizationCodeResult(
        access_token="AT",
        refresh_token="RT",
        id_token=_id_token({"account_id": "x"}),
        expires_in=3600,
        scope="x",
    )
    await svc.complete_oauth(flow_id=started.flow_id, code="C", state=started.state_token)

    # Second callback against the same flow.
    with pytest.raises(service_module.ClaudeOauthFlowError) as exc:
        await svc.complete_oauth(flow_id=started.flow_id, code="C2", state=started.state_token)
    assert exc.value.code == "flow_not_pending"


async def test_complete_oauth_logs_no_secrets(caplog: pytest.LogCaptureFixture) -> None:
    """Regression guard: no log line carries a real code/state/token value."""
    caplog.set_level(logging.DEBUG)
    svc, client, _ = _make_service()
    started = await svc.start_oauth()
    client.next_result = ClaudeAuthorizationCodeResult(
        access_token="SECRET_AT",
        refresh_token="SECRET_RT",
        id_token=_id_token({"account_id": "x"}),
        expires_in=3600,
        scope="x",
    )
    await svc.complete_oauth(flow_id=started.flow_id, code="SECRET_CODE", state=started.state_token)

    joined = "\n".join(rec.getMessage() for rec in caplog.records)
    for secret in ("SECRET_AT", "SECRET_RT", "SECRET_CODE"):
        assert secret not in joined, f"log leaked token material: {secret!r}"


# ---------------------------------------------------------------------------
# code#state paste format (see openspec/changes/fix-claude-oauth-account-claims)
# ---------------------------------------------------------------------------


async def test_complete_oauth_code_state_paste_with_matching_state_succeeds() -> None:
    """Anthropic's OOB page renders the response as ``<code>#<state>``. Operators
    paste that whole string into the dialog. The service MUST split on the
    first ``#``, validate the state segment against the flow's stored
    ``state_token``, and use only the code segment for the exchange.
    """
    svc, client, mgr = _make_service()
    started = await svc.start_oauth()
    client.next_result = ClaudeAuthorizationCodeResult(
        access_token="AT",
        refresh_token="RT",
        id_token=_id_token({"account_id": "acct-1"}),
        expires_in=3600,
        scope="user:inference",
    )

    resp = await svc.complete_oauth(
        flow_id=started.flow_id,
        code=f"AUTH_CODE#{started.state_token}",
        state=started.state_token,
    )

    assert resp.status == "success"
    # Client received only the code half, not the state half.
    assert client.last_code == "AUTH_CODE"
    assert client.last_state == started.state_token
    assert mgr.last_claims.claude_account_uuid == "acct-1"


async def test_complete_oauth_code_state_paste_with_mismatched_state_raises_state_mismatch() -> None:
    """If the state segment of ``code#state`` does not equal the flow's
    stored ``state_token``, the callback MUST reject with HTTP 400 and
    ``state_mismatch`` (and MUST NOT call the token endpoint).
    """
    svc, client, _ = _make_service()
    started = await svc.start_oauth()

    with pytest.raises(service_module.ClaudeOauthFlowError) as exc:
        await svc.complete_oauth(
            flow_id=started.flow_id,
            code="AUTH_CODE#SOMEONE_ELSES_STATE",
            state=started.state_token,
        )
    assert exc.value.code == "state_mismatch"
    assert client.last_code is None, "token endpoint MUST NOT be called"


async def test_complete_oauth_code_state_paste_with_whitespace_state_trimmed() -> None:
    """Trailing whitespace on the state segment (copied from Anthropic's
    page with stray newlines) MUST be tolerated via ``strip()``.
    """
    svc, client, _ = _make_service()
    started = await svc.start_oauth()
    client.next_result = ClaudeAuthorizationCodeResult(
        access_token="AT",
        refresh_token="RT",
        id_token=_id_token({"account_id": "acct-1"}),
        expires_in=3600,
        scope="x",
    )

    resp = await svc.complete_oauth(
        flow_id=started.flow_id,
        code=f"AUTH_CODE#{started.state_token}  \n",
        state=started.state_token,
    )
    assert resp.status == "success"


# ---------------------------------------------------------------------------
# Anthropic account-shape response (see openspec/changes/fix-claude-oauth-account-claims)
# ---------------------------------------------------------------------------


async def test_complete_oauth_anthropic_account_shape_response_succeeds() -> None:
    """Anthropic's actual token response carries identity in
    ``account.uuid`` + ``account.email_address`` + ``organization.uuid`` and
    does NOT include an ``id_token``. The service MUST accept this shape
    and build ``ClaudeOauthClaims`` directly from the JSON fields.
    """
    svc, client, mgr = _make_service()
    started = await svc.start_oauth()
    client.next_result = ClaudeAuthorizationCodeResult(
        access_token="sk-ant-oat01-AT",
        refresh_token="sk-ant-ort01-RT",
        id_token=None,
        expires_in=28800,
        scope="user:inference user:profile",
        account_uuid="491c2857-30eb-49ce-ad07-2b601efa041d",
        account_email="kusanat5@gmail.com",
        organization_uuid="cb355b7e-1b37-441c-8e2f-6f230a65a773",
        organization_name="kusanat5@gmail.com's Organization",
    )

    resp = await svc.complete_oauth(flow_id=started.flow_id, code="AUTH_CODE", state=started.state_token)

    assert resp.status == "success"
    assert mgr.last_claims.claude_account_uuid == "491c2857-30eb-49ce-ad07-2b601efa041d"
    assert mgr.last_claims.user_email == "kusanat5@gmail.com"
    assert mgr.last_claims.user_organization_uuid == "cb355b7e-1b37-441c-8e2f-6f230a65a773"
    assert mgr.last_claims.scopes == ["user:inference", "user:profile"]
    # raw_claims preserves the source so downstream consumers can audit
    assert mgr.last_claims.raw_claims["source"] == "anthropic_token_response"


async def test_complete_oauth_no_id_token_and_no_account_raises_id_token_missing() -> None:
    """Genuine "no identity payload" — neither ``id_token`` nor
    ``account.uuid`` + ``account.email_address`` — still raises
    ``id_token_missing``. The error_code contract is unchanged for this
    case; only the account-shape fallback is new.
    """
    svc, client, _ = _make_service()
    started = await svc.start_oauth()
    client.next_result = ClaudeAuthorizationCodeResult(
        access_token="AT",
        refresh_token="RT",
        id_token=None,
        expires_in=3600,
        scope="x",
        # account_uuid / account_email default to None → no identity payload
    )

    with pytest.raises(service_module.ClaudeOauthFlowError) as exc:
        await svc.complete_oauth(flow_id=started.flow_id, code="C", state=started.state_token)
    assert exc.value.code == "id_token_missing"


async def test_complete_oauth_account_shape_missing_email_still_raises_id_token_missing() -> None:
    """Account UUID present but no email → not enough to identify the
    account. Raise ``id_token_missing`` (preserves the contract that the
    service surfaces the missing-identity condition consistently).
    """
    svc, client, _ = _make_service()
    started = await svc.start_oauth()
    client.next_result = ClaudeAuthorizationCodeResult(
        access_token="AT",
        refresh_token="RT",
        id_token=None,
        expires_in=3600,
        scope="x",
        account_uuid="491c2857-30eb-49ce-ad07-2b601efa041d",
        # account_email omitted
    )

    with pytest.raises(service_module.ClaudeOauthFlowError) as exc:
        await svc.complete_oauth(flow_id=started.flow_id, code="C", state=started.state_token)
    assert exc.value.code == "id_token_missing"


# ---------------------------------------------------------------------------
# Diagnostic logging (see openspec/changes/fix-claude-oauth-account-claims)
# ---------------------------------------------------------------------------


async def test_complete_oauth_emits_callback_diagnostic_warning(caplog: pytest.LogCaptureFixture) -> None:
    """``claude.oauth.flow.callback.diagnostic`` MUST fire on every callback
    with flow_id, code length, code head/tail, state prefix, and the
    bool states_match flag. Surfaces as ``extra={...}`` so the production
    JSON formatter preserves structured fields.
    """
    svc, client, _ = _make_service()
    started = await svc.start_oauth()
    client.next_result = ClaudeAuthorizationCodeResult(
        access_token="AT",
        refresh_token="RT",
        id_token=_id_token({"account_id": "x"}),
        expires_in=3600,
        scope="x",
    )

    with caplog.at_level(logging.WARNING, logger="app.modules.claude.oauth.service"):
        await svc.complete_oauth(flow_id=started.flow_id, code="SECRET_AUTH_CODE_123456", state=started.state_token)

    records = [r for r in caplog.records if r.message == "claude.oauth.flow.callback.diagnostic"]
    assert records, "expected diagnostic warning"
    rec = records[0]
    assert getattr(rec, "flow_id") == started.flow_id
    assert getattr(rec, "code_len") == len("SECRET_AUTH_CODE_123456")
    assert getattr(rec, "code_head") == "SECRET_A"
    # state values are 43-char token_urlsafe; check prefix matches
    assert getattr(rec, "flow_state_prefix") == started.state_token[:8]
    assert getattr(rec, "submitted_state_prefix") == started.state_token[:8]
    assert getattr(rec, "states_match") is True


async def test_complete_oauth_id_token_missing_emits_raw_body_log(caplog: pytest.LogCaptureFixture) -> None:
    """``id_token_missing`` MUST also emit
    ``claude.oauth.flow.id_token_missing`` with the raw response body
    excerpt so the next incident is root-causible from logs alone.
    """
    svc, client, _ = _make_service()
    started = await svc.start_oauth()
    raw = b'{"access_token":"AT","refresh_token":"RT","expires_in":3600}'
    client.next_result = ClaudeAuthorizationCodeResult(
        access_token="AT",
        refresh_token="RT",
        id_token=None,
        expires_in=3600,
        scope="x",
        raw_body=raw,
    )

    with caplog.at_level(logging.ERROR, logger="app.modules.claude.oauth.service"):
        with pytest.raises(service_module.ClaudeOauthFlowError):
            await svc.complete_oauth(flow_id=started.flow_id, code="C", state=started.state_token)

    records = [r for r in caplog.records if r.message == "claude.oauth.flow.id_token_missing"]
    assert records, "expected id_token_missing error log"
    assert getattr(records[0], "raw_body_excerpt") == raw.decode("utf-8")


# ---------------------------------------------------------------------------
# claude-oauth-link endpoints (see openspec/changes/fix-claude-oauth-link-endpoints)
# ---------------------------------------------------------------------------


def _production_like_settings() -> Any:
    """Settings shaped exactly like the production defaults.

    Mirrors the values documented in ``app/core/config/settings.py`` after the
    fix in ``openspec/changes/fix-claude-oauth-link-endpoints``. Used by the
    URL-shape tests below so a regression in the default values is caught
    here instead of at operator runtime.
    """

    return SimpleNamespace(
        claude_oauth_authorize_endpoint="https://claude.com/cai/oauth/authorize",
        claude_oauth_client_id="9d1c250a-e61b-44d9-88ed-5944d1962f5e",
        claude_oauth_redirect_uri="https://platform.claude.com/oauth/code/callback",
        claude_oauth_scopes="user:profile user:inference",
        claude_oauth_flow_ttl_seconds=600,
    )


async def test_start_oauth_emits_claude_code_cli_url_with_code_true_flag() -> None:
    """Regression guard for openspec/.../fix-claude-oauth-link-endpoints.

    Anthropic accepts the authorization request only when the URL matches the
    Claude Code CLI pattern: ``https://claude.com/cai/oauth/authorize?code=true&...
    &redirect_uri=https%3A%2F%2Fplatform.claude.com%2Foauth%2Fcode%2Fcallback&...``.
    A previous attempt used ``https://platform.claude.com/oauth/authorize`` plus
    ``redirect_uri=https://console.anthropic.com/oauth/code`` and was rejected
    with "Redirect URI ... is not supported by client." (operator report).
    """
    from urllib.parse import parse_qs, urlparse

    svc, _, _ = _make_service(settings=_production_like_settings())
    resp = await svc.start_oauth()

    parsed = urlparse(resp.authorization_url)
    # Authorize endpoint matches Claude Code CLI.
    assert parsed.scheme == "https"
    assert parsed.netloc == "claude.com"
    assert parsed.path == "/cai/oauth/authorize"
    qs = parse_qs(parsed.query)
    # ``code=true`` must be the first query parameter (matches Claude Code CLI).
    assert qs.get("code") == ["true"], "code=true is required to select Anthropic's OOB code-display flow"
    # The order of the query string matters because Anthropic's authorize
    # endpoint requires ``code=true`` first; assert the literal substring
    # appears right after the question mark.
    assert resp.authorization_url.startswith("https://claude.com/cai/oauth/authorize?code=true&")
    # Redirect URI is the one Anthropic has whitelisted for the public
    # Claude Code client_id.
    assert qs.get("redirect_uri") == ["https://platform.claude.com/oauth/code/callback"]
    assert qs.get("client_id") == ["9d1c250a-e61b-44d9-88ed-5944d1962f5e"]
    assert qs.get("response_type") == ["code"]
    assert qs.get("code_challenge_method") == ["S256"]
    assert resp.redirect_uri == "https://platform.claude.com/oauth/code/callback"


def test_default_settings_pin_claude_code_compatible_endpoints(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Pin the production defaults so they cannot drift back to the rejected values.

    If either default regresses, operators will hit Anthropic's
    "Redirect URI ... is not supported by client" error at runtime.
    """
    from pydantic_settings import SettingsConfigDict

    from app.core.config.settings import Settings

    # Clear process env vars AND .env file load path so a developer-local
    # `.env` containing a stale override cannot shadow the pinned defaults.
    # Using the same `_env_file` override pattern as
    # `tests/unit/test_settings_home_dir.py::_settings_from_env_file`, but
    # pointing at an empty tmp_path file so neither `.env` nor `.env.local`
    # in BASE_DIR is consulted (Settings model_config has env_file=ENV_FILES).
    monkeypatch.delenv("CODEX_LB_CLAUDE_OAUTH_AUTHORIZE_ENDPOINT", raising=False)
    monkeypatch.delenv("CODEX_LB_CLAUDE_OAUTH_REDIRECT_URI", raising=False)

    empty_env = tmp_path / "empty.env"
    empty_env.touch()

    # Subclassing overrides `env_file` in the model_config to None so the
    # parent class's `env_file=ENV_FILES` setting is bypassed entirely.
    class _IsolatedSettings(Settings):
        model_config = SettingsConfigDict(
            env_prefix="CODEX_LB_",
            env_file=None,
            env_file_encoding="utf-8",
            extra="ignore",
        )

    settings = _IsolatedSettings(_env_file=empty_env)  # ty: ignore[unknown-argument]
    assert settings.claude_oauth_authorize_endpoint == "https://claude.com/cai/oauth/authorize"
    assert settings.claude_oauth_redirect_uri == "https://platform.claude.com/oauth/code/callback"
