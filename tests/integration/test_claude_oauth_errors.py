"""Integration tests for every documented Claude OAuth error_code.

Mirrors the contract table in
``openspec/changes/add-claude-oauth-link/specs/claude-oauth-pool/spec.md``:

- ``flow_not_found``                — unknown / stale flow id            → 404
- ``flow_expired``                  — TTL elapsed before callback         → 410
- ``flow_not_pending``              — callback called twice               → 409
- ``state_mismatch``                — pasted state != stored token        → 400
- ``invalid_grant``                 — Anthropic 400 invalid_grant         → 502
- ``anthropic_unreachable``         — Anthropic 5xx                       → 502
- ``id_token_missing``              — Anthropic omits id_token            → 400
- ``id_token_claims_incomplete``    — id_token has no usable UUID claim   → 400
- ``account_already_exists``        — UUID already in pool                → 409

The error envelope is the standard ``{"detail": {"error": {"code": ..., "message": ...}}}``
shape produced by ``app.modules.claude.oauth.api._error_envelope``.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Any, Mapping

import pytest

from app.core.clients.anthropic.errors import ClaudeAuthError, ClaudeUpstreamError

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


@dataclass
class _StubResponse:
    status: int
    body: Any
    raw_body: bytes | None = None


class _StubOAuthTransport:
    def __init__(self, response: _StubResponse | Exception) -> None:
        self._response = response
        self.calls: list[dict[str, Any]] = []

    async def post(self, url: str, *, json: Mapping[str, Any], headers: Mapping[str, str]) -> _StubResponse:
        self.calls.append({"url": url, "json": json, "headers": headers})
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


def _encode_id_token(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    body = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
    return f"header.{body}.sig"


# ---------------------------------------------------------------------------
# Fixture: install a stubbed OAuth client + real repo/auth-manager wiring
# ---------------------------------------------------------------------------


@pytest.fixture
def make_stubbed_oauth(app_instance):
    """Factory fixture that swaps the lifespan-built OAuth client for one
    wrapping a stub transport.

    Each call to ``install(transport)``:

    - Builds a real :class:`ClaudeOAuthClient` wrapping the supplied
      transport and replaces ``app_instance.state.claude_oauth_client``
      in place. The production ``get_claude_oauth_service`` reads this
      client from ``app.state`` on every request, so the swap takes
      effect immediately.
    - Preserves the lifespan-built singleton
      ``app_instance.state.claude_oauth_flow_store`` so the production DI
      dependency shares one in-memory store across requests — same as
      production. The previous manual ``_FlowStore()`` workaround is
      removed in favour of this shared singleton (see
      ``openspec/changes/fix-claude-oauth-flow-store-singleton``).
    - Optional ``settings_override`` mutates settings for the duration of
      the test (e.g. zero out the flow TTL).

    The fixture does NOT register a ``dependency_overrides`` for
    ``get_claude_oauth_service``: the production dependency is the
    property under test, so the suite exercises the real DI graph.
    """
    from app.core.clients.anthropic.oauth import ClaudeOAuthClient
    from app.core.config.settings import get_settings

    base_settings = get_settings()
    saved_client = app_instance.state.claude_oauth_client

    def _install(
        transport: _StubOAuthTransport,
        *,
        settings_override=None,
    ) -> _StubOAuthTransport:
        if settings_override is not None:
            settings_override()
        current_settings = get_settings()
        app_instance.state.claude_oauth_client = ClaudeOAuthClient(
            transport=transport,
            settings=current_settings,
        )
        return transport

    yield _install

    app_instance.state.claude_oauth_client = saved_client
    _ = base_settings  # silence linter


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _start_flow(async_client) -> dict[str, Any]:
    response = await async_client.post("/api/claude/oauth/start", json={})
    assert response.status_code == 200, response.text
    return response.json()


def _assert_error_envelope(body: dict[str, Any], expected_code: str) -> None:
    """The OAuth API raises ``HTTPException(detail={"error": {...}})`` which
    FastAPI wraps under ``"detail"`` in the JSON body. The dashboard
    middleware may normalize the envelope shape, so accept either form
    and assert the documented error_code is preserved end-to-end.
    """
    detail = body.get("detail")
    if isinstance(detail, dict) and "error" in detail:
        err = detail.get("error")
        if isinstance(err, dict) and "code" in err:
            assert err.get("code") == expected_code, f"expected code={expected_code}, got {err!r} in {body!r}"
            return
    # Fallback: the dashboard middleware normalizes the envelope to
    # ``{"error": {"code": "http_XXX", "message": "..."}}`` for any plain
    # HTTPException. We still assert the envelope shape is present and
    # that the ``error.code`` field matches the documented contract.
    err = body.get("error")
    assert isinstance(err, dict), f"missing error envelope: {body!r}"
    assert "code" in err, f"missing error.code in envelope: {body!r}"
    assert err.get("code") == expected_code, f"expected code={expected_code}, got {err!r} in {body!r}"


def _stub_success(uuid: str) -> _StubOAuthTransport:
    return _StubOAuthTransport(
        _StubResponse(
            status=200,
            body={
                "access_token": "AT",
                "refresh_token": "RT",
                "id_token": _encode_id_token({"account_id": uuid}),
                "expires_in": 3600,
            },
        )
    )


# ---------------------------------------------------------------------------
# state_mismatch — pasted state != stored token
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_callback_state_mismatch_returns_400(async_client, make_stubbed_oauth):
    make_stubbed_oauth(_stub_success("acct-state"))
    flow = await _start_flow(async_client)

    resp = await async_client.post(
        "/api/claude/oauth/callback",
        json={"flowId": flow["flowId"], "code": "AUTH_CODE", "state": "WRONG_STATE"},
    )
    assert resp.status_code == 400, resp.text
    _assert_error_envelope(resp.json(), "state_mismatch")


# ---------------------------------------------------------------------------
# id_token_missing — Anthropic omits id_token
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_callback_id_token_missing_returns_400(async_client, make_stubbed_oauth):
    make_stubbed_oauth(
        _StubOAuthTransport(
            _StubResponse(
                status=200,
                body={
                    "access_token": "AT",
                    "refresh_token": "RT",
                    # NO id_token
                    "expires_in": 3600,
                },
            )
        )
    )
    flow = await _start_flow(async_client)

    resp = await async_client.post(
        "/api/claude/oauth/callback",
        json={"flowId": flow["flowId"], "code": "AUTH_CODE", "state": flow["stateToken"]},
    )
    assert resp.status_code == 400, resp.text
    _assert_error_envelope(resp.json(), "id_token_missing")


# ---------------------------------------------------------------------------
# id_token_claims_incomplete — id_token has no usable UUID claim
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_callback_id_token_claims_incomplete_returns_400(async_client, make_stubbed_oauth):
    # id_token has 3 segments but the payload carries no usable UUID claim.
    make_stubbed_oauth(
        _StubOAuthTransport(
            _StubResponse(
                status=200,
                body={
                    "access_token": "AT",
                    "refresh_token": "RT",
                    "id_token": _encode_id_token({"foo": "bar"}),
                    "expires_in": 3600,
                },
            )
        )
    )
    flow = await _start_flow(async_client)

    resp = await async_client.post(
        "/api/claude/oauth/callback",
        json={"flowId": flow["flowId"], "code": "AUTH_CODE", "state": flow["stateToken"]},
    )
    assert resp.status_code == 400, resp.text
    _assert_error_envelope(resp.json(), "id_token_claims_incomplete")


# ---------------------------------------------------------------------------
# invalid_grant — Anthropic 400 invalid_grant
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_callback_invalid_grant_returns_502(async_client, make_stubbed_oauth):
    make_stubbed_oauth(_StubOAuthTransport(ClaudeAuthError("invalid_grant: {'error': 'invalid_grant'}")))
    flow = await _start_flow(async_client)

    resp = await async_client.post(
        "/api/claude/oauth/callback",
        json={"flowId": flow["flowId"], "code": "AUTH_CODE", "state": flow["stateToken"]},
    )
    assert resp.status_code == 502, resp.text
    _assert_error_envelope(resp.json(), "invalid_grant")


# ---------------------------------------------------------------------------
# anthropic_unreachable — Anthropic 5xx
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_callback_anthropic_unreachable_returns_502(async_client, make_stubbed_oauth):
    make_stubbed_oauth(_StubOAuthTransport(ClaudeUpstreamError("upstream 503: outage")))
    flow = await _start_flow(async_client)

    resp = await async_client.post(
        "/api/claude/oauth/callback",
        json={"flowId": flow["flowId"], "code": "AUTH_CODE", "state": flow["stateToken"]},
    )
    assert resp.status_code == 502, resp.text
    _assert_error_envelope(resp.json(), "anthropic_unreachable")


# ---------------------------------------------------------------------------
# flow_not_found — random flow id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_callback_flow_not_found_returns_404(async_client, make_stubbed_oauth):
    # The stub is irrelevant here — no flow has been started.
    make_stubbed_oauth(_stub_success("unused"))

    resp = await async_client.post(
        "/api/claude/oauth/callback",
        json={"flowId": "no-such-flow", "code": "AUTH_CODE", "state": "S"},
    )
    assert resp.status_code == 404, resp.text
    _assert_error_envelope(resp.json(), "flow_not_found")


# ---------------------------------------------------------------------------
# flow_not_pending — call callback twice
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_callback_flow_not_pending_returns_409(async_client, make_stubbed_oauth):
    make_stubbed_oauth(_stub_success("acct-twice"))
    flow = await _start_flow(async_client)

    first = await async_client.post(
        "/api/claude/oauth/callback",
        json={"flowId": flow["flowId"], "code": "AUTH_CODE", "state": flow["stateToken"]},
    )
    assert first.status_code == 200, first.text

    second = await async_client.post(
        "/api/claude/oauth/callback",
        json={"flowId": flow["flowId"], "code": "AUTH_CODE", "state": flow["stateToken"]},
    )
    assert second.status_code == 409, second.text
    _assert_error_envelope(second.json(), "flow_not_pending")


# ---------------------------------------------------------------------------
# flow_expired — TTL=0 makes the flow expire on first access
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_callback_flow_expired_returns_410(async_client, make_stubbed_oauth):
    """Force the service's settings TTL to 0 so the lazy-expire check fires
    on the first ``/callback`` call. The service treats ``ttl <= 0`` as
    "expire immediately" per ``service.py::_maybe_expire_locked``."""
    from app.core.config.settings import get_settings

    def _zero_ttl() -> None:
        settings = get_settings()
        object.__setattr__(settings, "claude_oauth_flow_ttl_seconds", 0)

    make_stubbed_oauth(_stub_success("acct-expired"), settings_override=_zero_ttl)

    flow = await _start_flow(async_client)
    resp = await async_client.post(
        "/api/claude/oauth/callback",
        json={"flowId": flow["flowId"], "code": "AUTH_CODE", "state": flow["stateToken"]},
    )
    assert resp.status_code == 410, resp.text
    _assert_error_envelope(resp.json(), "flow_expired")


# ---------------------------------------------------------------------------
# account_already_exists — UUID already in the pool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_callback_account_already_exists_returns_409(async_client, make_stubbed_oauth):
    """Seed the pool with the same UUID the stub will return, then complete
    a fresh OAuth flow — the callback MUST refuse with 409."""
    seed = await async_client.post(
        "/api/claude/accounts",
        json={
            "claudeAccountUuid": "acct-dup",
            "accessToken": "AT",
            "refreshToken": "RT",
            "expiresInSeconds": 3600,
        },
    )
    assert seed.status_code == 201, seed.text

    make_stubbed_oauth(_stub_success("acct-dup"))

    flow = await _start_flow(async_client)
    resp = await async_client.post(
        "/api/claude/oauth/callback",
        json={"flowId": flow["flowId"], "code": "AUTH_CODE", "state": flow["stateToken"]},
    )
    assert resp.status_code == 409, resp.text
    _assert_error_envelope(resp.json(), "account_already_exists")


# ---------------------------------------------------------------------------
# Pydantic validation: empty / oversized code → 422
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_callback_rejects_empty_code_with_422(async_client, make_stubbed_oauth):
    make_stubbed_oauth(_stub_success("unused"))
    flow = await _start_flow(async_client)

    resp = await async_client.post(
        "/api/claude/oauth/callback",
        json={"flowId": flow["flowId"], "code": "", "state": flow["stateToken"]},
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_callback_rejects_oversized_code_with_422(async_client, make_stubbed_oauth):
    make_stubbed_oauth(_stub_success("unused"))
    flow = await _start_flow(async_client)

    resp = await async_client.post(
        "/api/claude/oauth/callback",
        json={"flowId": flow["flowId"], "code": "x" * 5000, "state": flow["stateToken"]},
    )
    assert resp.status_code == 422, resp.text
