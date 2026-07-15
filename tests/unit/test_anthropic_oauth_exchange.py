"""Tests for ``ClaudeOAuthClient.exchange_authorization_code``.

Mirror tests for ``refresh``: same ``_Response`` / ``_Transport`` shape, same
status-code / error-class mapping. We do not duplicate the full file; this one
focuses on the new flow and on the differences the new method introduces
(tolerated-missing id_token, code+verifier+redirect_uri request shape).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Mapping

import pytest

from app.core.clients.anthropic.errors import ClaudeAPIError, ClaudeAuthError, ClaudeUpstreamError
from app.core.clients.anthropic.oauth import (
    ClaudeAuthorizationCodeResult,
    ClaudeOAuthClient,
)

pytestmark = pytest.mark.unit


class _Response:
    def __init__(self, status: int, body: dict) -> None:
        self.status = status
        self.body = body

    async def json(self) -> dict:
        return self.body


class _Transport:
    def __init__(self, response: _Response) -> None:
        self.response = response
        self.last_url: str | None = None
        self.last_json: Mapping[str, Any] | None = None
        self.last_headers: Mapping[str, str] | None = None

    async def post(
        self,
        url: str,
        *,
        json: Mapping[str, Any],
        headers: Mapping[str, str],
    ) -> _Response:
        self.last_url = url
        self.last_json = json
        self.last_headers = headers
        return self.response


@pytest.fixture()
def settings() -> SimpleNamespace:
    return SimpleNamespace(
        claude_oauth_token_endpoint="https://auth.example.test/oauth/token",
        claude_oauth_extra_headers={"X-Client": "codex-lb"},
    )


async def test_exchange_authorization_code_returns_full_result(settings: SimpleNamespace) -> None:
    resp = _Response(
        status=200,
        body={
            "access_token": "AT",
            "refresh_token": "RT",
            "id_token": "JWT.PAYLOAD.SIG",
            "expires_in": 3600,
            "scope": "user:profile user:inference",
            "token_type": "Bearer",
        },
    )
    t = _Transport(resp)
    client = ClaudeOAuthClient(transport=t, settings=settings)

    out = await client.exchange_authorization_code(
        code="AUTH_CODE", code_verifier="VERIFIER", state="STATE", redirect_uri="https://redirect.example/cb"
    )

    assert isinstance(out, ClaudeAuthorizationCodeResult)
    assert out.access_token == "AT"
    assert out.refresh_token == "RT"
    assert out.id_token == "JWT.PAYLOAD.SIG"
    assert out.expires_in == 3600
    assert out.scope == "user:profile user:inference"

    # Request body shape per design.md
    assert t.last_json == {
        "grant_type": "authorization_code",
        "code": "AUTH_CODE",
        "code_verifier": "VERIFIER",
        "state": "STATE",
        "client_id": client._client_id,
        "redirect_uri": "https://redirect.example/cb",
    }
    # URL is the configured endpoint
    assert t.last_url == "https://auth.example.test/oauth/token"


async def test_exchange_authorization_code_tolerates_missing_id_token(settings: SimpleNamespace) -> None:
    resp = _Response(
        status=200,
        body={"access_token": "AT", "refresh_token": "RT", "expires_in": 3600, "scope": "x"},
    )
    t = _Transport(resp)
    client = ClaudeOAuthClient(transport=t, settings=settings)

    out = await client.exchange_authorization_code(
        code="AUTH_CODE", code_verifier="VERIFIER", state="STATE", redirect_uri="https://redirect.example/cb"
    )

    assert out.id_token is None
    assert out.access_token == "AT"


async def test_exchange_authorization_code_invalid_grant_raises_auth_error(settings: SimpleNamespace) -> None:
    resp = _Response(status=400, body={"error": "invalid_grant"})
    t = _Transport(resp)
    client = ClaudeOAuthClient(transport=t, settings=settings)

    with pytest.raises(ClaudeAuthError):
        await client.exchange_authorization_code(
            code="BAD", code_verifier="V", state="S", redirect_uri="https://r.example/cb"
        )


async def test_exchange_authorization_code_5xx_raises_upstream_error(settings: SimpleNamespace) -> None:
    resp = _Response(status=503, body={"error": "temporarily_unavailable"})
    t = _Transport(resp)
    client = ClaudeOAuthClient(transport=t, settings=settings)

    with pytest.raises(ClaudeUpstreamError):
        await client.exchange_authorization_code(
            code="C", code_verifier="V", state="S", redirect_uri="https://r.example/cb"
        )


async def test_exchange_authorization_code_malformed_body_raises_api_error(settings: SimpleNamespace) -> None:
    resp = _Response(status=200, body={"access_token": "AT"})  # missing refresh_token + expires_in
    t = _Transport(resp)
    client = ClaudeOAuthClient(transport=t, settings=settings)

    with pytest.raises(ClaudeAPIError):
        await client.exchange_authorization_code(
            code="C", code_verifier="V", state="S", redirect_uri="https://r.example/cb"
        )


# ---------------------------------------------------------------------------
# Anthropic actual-shape response (no id_token, identity in account.* /
# organization.*) — see openspec/changes/fix-claude-oauth-account-claims
# ---------------------------------------------------------------------------


async def test_exchange_authorization_code_populates_account_shape_fields(settings: SimpleNamespace) -> None:
    """Anthropic's actual token response carries identity in
    ``account.{uuid, email_address}`` + ``organization.{uuid, name}``,
    not in an OIDC id_token. The OAuth client must surface those fields
    so the service can build ``ClaudeOauthClaims`` without id_token.
    """
    resp = _Response(
        status=200,
        body={
            "token_type": "Bearer",
            "access_token": "sk-ant-oat01-AT",
            "expires_in": 28800,
            "refresh_token": "sk-ant-ort01-RT",
            "scope": "user:inference user:profile",
            "token_uuid": "7f7a49a7-dd42-4f17-96fc-d8f115cd68f5",
            "refresh_token_expires_in": 2502728,
            "organization": {
                "uuid": "cb355b7e-1b37-441c-8e2f-6f230a65a773",
                "name": "kusanat5@gmail.com's Organization",
            },
            "account": {
                "uuid": "491c2857-30eb-49ce-ad07-2b601efa041d",
                "email_address": "kusanat5@gmail.com",
            },
        },
    )
    t = _Transport(resp)
    client = ClaudeOAuthClient(transport=t, settings=settings)

    out = await client.exchange_authorization_code(
        code="AUTH_CODE", code_verifier="V", state="S", redirect_uri="https://r.example/cb"
    )

    assert out.id_token is None
    assert out.account_uuid == "491c2857-30eb-49ce-ad07-2b601efa041d"
    assert out.account_email == "kusanat5@gmail.com"
    assert out.organization_uuid == "cb355b7e-1b37-441c-8e2f-6f230a65a773"
    assert out.organization_name == "kusanat5@gmail.com's Organization"
    # Required fields stay populated
    assert out.access_token == "sk-ant-oat01-AT"
    assert out.refresh_token == "sk-ant-ort01-RT"
    assert out.expires_in == 28800
    assert out.scope == "user:inference user:profile"


async def test_exchange_authorization_code_account_shape_backward_compat(settings: SimpleNamespace) -> None:
    """A body without ``account`` / ``organization`` keys (legacy) leaves
    the new fields ``None`` and id_token untouched.
    """
    resp = _Response(
        status=200,
        body={
            "access_token": "AT",
            "refresh_token": "RT",
            "id_token": "JWT.PAYLOAD.SIG",
            "expires_in": 3600,
        },
    )
    t = _Transport(resp)
    client = ClaudeOAuthClient(transport=t, settings=settings)

    out = await client.exchange_authorization_code(
        code="C", code_verifier="V", state="S", redirect_uri="https://r.example/cb"
    )

    assert out.id_token == "JWT.PAYLOAD.SIG"
    assert out.account_uuid is None
    assert out.account_email is None
    assert out.organization_uuid is None
    assert out.organization_name is None


async def test_exchange_authorization_code_tolerates_malformed_account_org(settings: SimpleNamespace) -> None:
    """``account`` or ``organization`` being null/non-dict must not raise;
    the client treats any of these as "no identity payload" (all four
    new fields ``None``).
    """
    for bad in (None, "not-a-dict", 42, ["list"]):
        resp = _Response(
            status=200,
            body={
                "access_token": "AT",
                "refresh_token": "RT",
                "expires_in": 3600,
                "account": bad,
                "organization": bad,
            },
        )
        t = _Transport(resp)
        client = ClaudeOAuthClient(transport=t, settings=settings)

        out = await client.exchange_authorization_code(
            code="C", code_verifier="V", state="S", redirect_uri="https://r.example/cb"
        )
        assert out.account_uuid is None
        assert out.account_email is None
        assert out.organization_uuid is None
        assert out.organization_name is None
