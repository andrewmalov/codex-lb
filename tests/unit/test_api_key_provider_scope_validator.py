"""Tests for the provider-scoped API key validator factory.

The factory in :mod:`app.modules.api_keys.provider_auth` wraps the existing
``validate_proxy_api_key`` dependency and rejects keys whose
``provider_scope`` does not include the requested provider. Two cases:

- codex-only key sent to a Claude route -> 403
- dual-scope (codex, claude) key sent to a Claude route -> returns key

The unit test mocks out the underlying validator and asserts the factory's
branch behavior at the dependency boundary, so it does NOT need a real
database session.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException

from app.modules.api_keys import provider_auth
from app.modules.api_keys.provider_auth import api_key_validator_with_provider


pytestmark = pytest.mark.unit


class _DummyRequest:
    """Minimal stand-in for :class:`fastapi.Request` accepted by the wrapped
    validator. The dependency only inspects request.headers via the
    underlying ``Security`` extraction, so an empty scope object is enough —
    the factory we test dispatches into our patched ``_existing_validate``
    function which does NOT touch the request."""
    def __init__(self) -> None:
        self.headers: dict[str, str] = {}


@pytest.fixture()
def patched_validator(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    captured: dict[str, Any] = {"key": None, "calls": 0}

    async def _fake(request: Any) -> Any:
        captured["calls"] += 1
        return captured["key"]

    monkeypatch.setattr(provider_auth, "_existing_validate_proxy_api_key", _fake)
    return captured


@pytest.mark.asyncio
async def test_codex_only_key_rejected_for_claude_route(patched_validator: dict[str, Any]) -> None:
    patched_validator["key"] = SimpleNamespace(
        id="key-codex-only",
        provider_scope="codex",
    )
    dep = api_key_validator_with_provider("claude")

    with pytest.raises(HTTPException) as exc_info:
        await dep(_DummyRequest())  # type: ignore[arg-type]

    assert exc_info.value.status_code == 403
    assert "claude" in str(exc_info.value.detail)
    assert patched_validator["calls"] == 1


@pytest.mark.asyncio
async def test_claude_only_key_accepted_for_claude_route(patched_validator: dict[str, Any]) -> None:
    expected = SimpleNamespace(id="key-claude-only", provider_scope="claude")
    patched_validator["key"] = expected
    dep = api_key_validator_with_provider("claude")

    result = await dep(_DummyRequest())  # type: ignore[arg-type]

    assert result is expected


@pytest.mark.asyncio
async def test_dual_scope_key_accepted_for_either_provider(patched_validator: dict[str, Any]) -> None:
    expected = SimpleNamespace(id="key-dual", provider_scope="codex,claude")
    patched_validator["key"] = expected
    dep = api_key_validator_with_provider("claude")

    result = await dep(_DummyRequest())  # type: ignore[arg-type]

    assert result is expected


@pytest.mark.asyncio
async def test_empty_scope_rejected(patched_validator: dict[str, Any]) -> None:
    patched_validator["key"] = SimpleNamespace(id="k", provider_scope="")
    dep = api_key_validator_with_provider("claude")

    with pytest.raises(HTTPException) as exc_info:
        await dep(_DummyRequest())  # type: ignore[arg-type]
    assert exc_info.value.status_code == 403
