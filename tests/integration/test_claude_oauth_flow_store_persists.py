"""Regression test for the OAuth flow-store singleton bug.

Closes the gap exposed by `tests/integration/test_claude_oauth_flow.py` and
`tests/integration/test_claude_oauth_errors.py` — both of those suites work
around the bug with a `dependency_overrides` that manually shares one
`_FlowStore` across every request. This suite does not override
`get_claude_oauth_service`; it uses the production dependency directly and
just swaps the OAuth transport stub on `app.state.claude_oauth_client`.

Without the fix (no singleton on `app.state.claude_oauth_flow_store`):

    1. Start request enters `get_claude_oauth_service`; a fresh
       `ClaudeOAuthService` is built with a fresh empty `_FlowStore`.
       Flow A is added to Store-1.
    2. Submit request enters `get_claude_oauth_service`; a fresh
       `ClaudeOAuthService` is built with a fresh empty `_FlowStore`
       (Store-2). Lookup of Flow A returns `None`.
    3. The dashboard would receive 404 `error_code: flow_not_found`.

With the fix:

    1. Start request enters `get_claude_oauth_service`; the flow is added
       to the singleton `_FlowStore` at `app.state.claude_oauth_flow_store`.
    2. Submit request enters `get_claude_oauth_service`; the singleton is
       reused; Flow A is found; token exchange runs; the account is
       persisted.

This is end-to-end black-box: it is the closest in-process reproduction of
the operator's reported "Authorization request not found. Please start over."
that does not require a multi-replica deployment.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Any, Mapping

import pytest

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


@dataclass
class _StubResponse:
    """Mimics the aiohttp ``ClientResponse`` shape consumed by
    :class:`ClaudeOAuthClient`. Only ``status`` and ``json()`` are read in
    production paths; ``raw_body`` is optional and used by the parser only
    when present.
    """

    status: int
    body: dict[str, Any]
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
# Fixture: stub the OAuth transport without overriding get_claude_oauth_service
# ---------------------------------------------------------------------------


@pytest.fixture
def stubbed_oauth_client(app_instance):
    """Install a stubbed ``ClaudeOAuthClient`` on ``app.state`` and yield the
    transport so tests can assert on the JSON body sent to Anthropic.

    Unlike :func:`tests.integration.test_claude_oauth_flow.stubbed_oauth_transport`,
    this fixture does NOT override
    :func:`app.modules.claude.oauth.api.get_claude_oauth_service`. The real
    production dependency runs end-to-end:

    - ``app.state.claude_oauth_client`` (lifespan-built) is swapped for
      one wrapping this stub transport.
    - ``app.state.claude_oauth_flow_store`` (lifespan-built) is read by
      the real DI dependency and passed to ``ClaudeOAuthService``.
    - One ``AsyncSession`` per request via ``get_session``.

    If the lifespan fails to install either collaborator, the real
    dependency surfaces a ``RuntimeError`` — that is the failure mode
    this regression test is designed to catch.
    """
    from app.core.clients.anthropic.oauth import ClaudeOAuthClient
    from app.core.config.settings import get_settings

    transport = _StubOAuthTransport(
        _StubResponse(
            status=200,
            body={
                "access_token": "sk-ant-oat01-STUB-AT",
                "refresh_token": "sk-ant-ort01-STUB-RT",
                "id_token": _encode_id_token(
                    {
                        "account_id": "acct-persists",
                        "email": "persists@example.test",
                        "scope": "user:profile user:inference",
                    }
                ),
                "expires_in": 3600,
                "scope": "user:profile user:inference",
            },
        )
    )

    settings = get_settings()
    saved_client = app_instance.state.claude_oauth_client
    app_instance.state.claude_oauth_client = ClaudeOAuthClient(
        transport=transport,
        settings=settings,
    )
    try:
        yield transport
    finally:
        app_instance.state.claude_oauth_client = saved_client


# ---------------------------------------------------------------------------
# The regression test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_then_callback_resolves_flow_via_real_di(async_client, stubbed_oauth_client) -> None:
    """Start creates a flow in the singleton store; callback resolves it.

    Before the singleton fix the callback would return 404
    ``error_code: flow_not_found`` because each request resolves a fresh
    ``ClaudeOAuthService`` with a fresh empty ``_FlowStore``.
    """
    # 1. Start the flow. The lifespan-built singleton `_FlowStore` is the
    # one production uses. Start inserts the new flow into it.
    start_response = await async_client.post("/api/claude/oauth/start", json={})
    assert start_response.status_code == 200, start_response.text
    start_body = start_response.json()
    flow_id = start_body["flowId"]
    state_token = start_body["stateToken"]

    # Defensive check: the lifespan must have installed the flow store
    # for the dependency to have produced a 200. If the singleton is
    # missing the dependency raises RuntimeError -> 500. We want 200 here
    # so this assertion also catches the "lifespan forgot to install the
    # flow store" regression.
    assert flow_id, "flow_id missing from /start response"
    assert state_token, "state_token missing from /start response"

    # 2. Sanity check: the status endpoint in a separate request must
    # also resolve the flow via the same singleton store. Without the
    # fix, the lookup would 404 flow_not_found here too.
    status_response = await async_client.get("/api/claude/oauth/status", params={"flowId": flow_id})
    assert status_response.status_code == 200, status_response.text
    status_body = status_response.json()
    assert status_body["status"] == "pending", status_body
    assert status_body.get("errorCode") in (None, "null"), status_body

    # 3. Submit the callback. This is the request that reproduces the
    # operator's reported bug. Without the singleton, this returns 404
    # `flow_not_found`; with the singleton, it returns 200 success and
    # the new account payload.
    callback_response = await async_client.post(
        "/api/claude/oauth/callback",
        json={
            "flowId": flow_id,
            "code": "PASTE-CODE-FROM-ANTHROPIC-PAGE",
            "state": state_token,
        },
    )
    assert callback_response.status_code == 200, callback_response.text
    callback_body = callback_response.json()
    assert callback_body["status"] == "success"
    assert callback_body["account"]["claudeAccountUuid"] == "acct-persists"
    assert callback_body["account"]["userEmail"] == "persists@example.test"


@pytest.mark.asyncio
async def test_status_lookup_resolves_flow_via_real_di(async_client, stubbed_oauth_client) -> None:
    """Companion regression test for :func:`test_start_then_callback_resolves_flow_via_real_di`.

    The Status endpoint, like Callback, resolves a different request than
    Start. Without the singleton, the status lookup also returns
    ``flow_not_found``. This test pins the property on the second route
    that depends on the same fix.
    """
    start_response = await async_client.post("/api/claude/oauth/start", json={})
    assert start_response.status_code == 200, start_response.text
    flow_id = start_response.json()["flowId"]

    status_response = await async_client.get("/api/claude/oauth/status", params={"flowId": flow_id})
    assert status_response.status_code == 200, status_response.text
    status_body = status_response.json()
    assert status_body["status"] == "pending", status_body
    # ``flow_not_found`` is the exact error code the bug used to surface as;
    # its absence here is the assertion that proves the singleton is shared.
    assert status_body.get("errorCode") in (None, "null"), status_body
