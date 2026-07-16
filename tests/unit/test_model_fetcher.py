from __future__ import annotations

import asyncio
import contextlib
from types import SimpleNamespace
from typing import Any, cast

import pytest

from app.core.clients.model_fetcher import ModelFetchError, fetch_claude_models, fetch_models_for_plan
from app.core.upstream_proxy import ResolvedProxyEndpoint, ResolvedUpstreamRoute

pytestmark = pytest.mark.unit


class _TimeoutResponse:
    status = 200

    async def __aenter__(self) -> "_TimeoutResponse":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def json(self, *, content_type: str | None = None) -> object:
        raise asyncio.TimeoutError


class _Session:
    def get(self, *args: object, **kwargs: object) -> _TimeoutResponse:
        return _TimeoutResponse()


class _VersionCache:
    async def get_version(self) -> str:
        return "0.128.0"


class _CodexResponse:
    status_code = 200

    def json(self) -> dict[str, object]:
        return {
            "models": [
                {
                    "slug": "gpt-5.2",
                    "display_name": "GPT-5.2",
                    "description": "model",
                    "base_instructions": "",
                    "context_window": 128000,
                    "priority": 1,
                }
            ]
        }


class _CodexClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def request(self, method: str, url: str, *, route: ResolvedUpstreamRoute, **kwargs: object) -> object:
        self.calls.append({"method": method, "url": url, "route": route, **kwargs})
        return _CodexResponse()


async def test_fetch_models_for_plan_maps_read_timeout_to_model_fetch_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "app.core.clients.model_fetcher.get_settings",
        lambda: SimpleNamespace(upstream_base_url="https://upstream.example"),
    )
    monkeypatch.setattr(
        "app.core.clients.model_fetcher.get_codex_version_cache",
        lambda: _VersionCache(),
    )

    @contextlib.asynccontextmanager
    async def lease_session():
        yield _Session()

    monkeypatch.setattr("app.core.clients.model_fetcher.lease_http_session", lease_session)

    with pytest.raises(ModelFetchError) as exc_info:
        await fetch_models_for_plan("access-token", "account-id", allow_direct_egress=True)

    assert exc_info.value.status_code == 504
    assert exc_info.value.message == "Upstream models API timed out"
    assert exc_info.value.transport_error is True


async def test_fetch_models_for_plan_uses_resolved_codex_route(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "app.core.clients.model_fetcher.get_settings",
        lambda: SimpleNamespace(upstream_base_url="https://upstream.example/backend-api"),
    )
    monkeypatch.setattr(
        "app.core.clients.model_fetcher.get_codex_version_cache",
        lambda: _VersionCache(),
    )
    route = ResolvedUpstreamRoute(
        mode="account_bound",
        pool_id="pool_1",
        endpoint=ResolvedProxyEndpoint("ep_1", "http", "proxy.test", 8080),
    )
    client = _CodexClient()

    models = await fetch_models_for_plan("access-token", "account-id", route=route, codex_client=cast(Any, client))

    assert [model.slug for model in models] == ["gpt-5.2"]
    assert client.calls[0]["route"] is route
    assert client.calls[0]["method"] == "GET"
    assert str(client.calls[0]["url"]).endswith("/codex/models?client_version=0.128.0")


# ---------------------------------------------------------------------------
# fetch_claude_models — see openspec/changes/fix-model-refresh-scheduler-provider-scope
# ---------------------------------------------------------------------------


class _ClaudeJsonOk:
    status = 200

    async def __aenter__(self) -> "_ClaudeJsonOk":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def text(self) -> str:
        return ""

    async def json(self, *, content_type: str | None = None) -> object:
        return {
            "data": [
                {"id": "claude-opus-4-20250514", "display_name": "Claude Opus 4", "type": "model"},
                {"id": "claude-sonnet-4-20250514", "display_name": "Claude Sonnet 4", "type": "model"},
            ]
        }


class _ClaudeOkSession:
    def __init__(self) -> None:
        self.last_url: str | None = None
        self.last_headers: dict[str, str] | None = None

    def get(self, url: str, *, headers: dict[str, str], **_: object) -> _ClaudeJsonOk:
        self.last_url = url
        self.last_headers = headers
        return _ClaudeJsonOk()


class _Claude401Response:
    status = 401

    async def __aenter__(self) -> "_Claude401Response":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def text(self) -> str:
        return '{"detail":"Could not parse your authentication token. Please try signing in again."}'


class _Claude401Session:
    def get(self, *args: object, **kwargs: object) -> _Claude401Response:
        return _Claude401Response()


class _ClaudeTimeoutSession:
    def get(self, *args: object, **kwargs: object) -> _TimeoutResponse:
        return _TimeoutResponse()


async def test_fetch_claude_models_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "app.core.clients.model_fetcher.get_settings",
        lambda: SimpleNamespace(claude_api_base_url="https://api.anthropic.com"),
    )
    session = _ClaudeOkSession()

    @contextlib.asynccontextmanager
    async def lease_session():
        yield session

    monkeypatch.setattr("app.core.clients.model_fetcher.lease_http_session", lease_session)

    models = await fetch_claude_models("sk-ant-oat01-AT", None, allow_direct_egress=True)

    assert [m.slug for m in models] == [
        "claude-opus-4-20250514",
        "claude-sonnet-4-20250514",
    ]
    assert [m.display_name for m in models] == ["Claude Opus 4", "Claude Sonnet 4"]
    assert {m.available_in_plans for m in models} == {frozenset({"claude_subscription"})}
    # URL hits Anthropic, not Codex upstream
    assert session.last_url == "https://api.anthropic.com/v1/models"
    # No chatgpt-account-id header is sent to Anthropic
    assert "chatgpt-account-id" not in (session.last_headers or {})
    # anthropic-version IS sent (Anthropic requires it)
    assert session.last_headers["anthropic-version"] == "2023-06-01"
    # Authorization Bearer is set
    assert session.last_headers["Authorization"] == "Bearer sk-ant-oat01-AT"


async def test_fetch_claude_models_401_raises_model_fetch_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "app.core.clients.model_fetcher.get_settings",
        lambda: SimpleNamespace(claude_api_base_url="https://api.anthropic.com"),
    )

    @contextlib.asynccontextmanager
    async def lease_session():
        yield _Claude401Session()

    monkeypatch.setattr("app.core.clients.model_fetcher.lease_http_session", lease_session)

    with pytest.raises(ModelFetchError) as exc_info:
        await fetch_claude_models("sk-ant-bad-token", None, allow_direct_egress=True)

    assert exc_info.value.status_code == 401
    assert "Could not parse" in exc_info.value.message
    assert exc_info.value.transport_error is False


async def test_fetch_claude_models_timeout_raises_transport_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "app.core.clients.model_fetcher.get_settings",
        lambda: SimpleNamespace(claude_api_base_url="https://api.anthropic.com"),
    )

    @contextlib.asynccontextmanager
    async def lease_session():
        yield _ClaudeTimeoutSession()

    monkeypatch.setattr("app.core.clients.model_fetcher.lease_http_session", lease_session)

    with pytest.raises(ModelFetchError) as exc_info:
        await fetch_claude_models("sk-ant-oat01-AT", None, allow_direct_egress=True)

    assert exc_info.value.status_code == 504
    assert "timed out" in exc_info.value.message
    assert exc_info.value.transport_error is True


async def test_fetch_claude_models_strips_trailing_slash_from_base(monkeypatch: pytest.MonkeyPatch) -> None:
    """``claude_api_base_url`` may carry a trailing slash; the URL builder
    MUST normalize so we don't hit ``//v1/models``.
    """
    monkeypatch.setattr(
        "app.core.clients.model_fetcher.get_settings",
        lambda: SimpleNamespace(claude_api_base_url="https://api.anthropic.com/"),
    )
    session = _ClaudeOkSession()

    @contextlib.asynccontextmanager
    async def lease_session():
        yield session

    monkeypatch.setattr("app.core.clients.model_fetcher.lease_http_session", lease_session)

    await fetch_claude_models("sk-ant-oat01-AT", None, allow_direct_egress=True)

    assert session.last_url == "https://api.anthropic.com/v1/models"
    assert "//v1" not in (session.last_url or "")
