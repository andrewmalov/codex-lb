"""Provider-scoped API key validator factory.

Source of truth: ``openspec/changes/add-claude-oauth-pool/specs/api-keys/spec.md``
— specifically the requirement that ``provider_scope`` MUST be honored on
proxy routes.

The existing ``validate_proxy_api_key`` dependency in
:mod:`app.core.auth.dependencies` authenticates the bearer token but does not
filter by provider scope. This module wraps it with a small factory that
returns a FastAPI dependency which raises HTTP 403 when the validated key
does not authorize the requested provider string.

Usage::

    from app.modules.api_keys.provider_auth import api_key_validator_with_provider

    _claude_key = api_key_validator_with_provider("claude")

    @router.post("/claude/v1/messages")
    async def messages(..., api_key=Depends(_claude_key)): ...

The factory is intentionally tiny: we deliberately do not duplicate any of the
existing validator's caching, header extraction, or error-mapping logic. The
dependency reuses the already-validated :class:`ApiKeyData` returned by the
existing dependency and only adds one provider-scope check on top of it.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request

from app.core.auth.dependencies import validate_proxy_api_key


async def _existing_validate_proxy_api_key(request: Request) -> Any:
    """Thin alias used by tests to patch in a stub validator.

    Production code calls :func:`validate_proxy_api_key` directly; the alias
    exists so unit tests can ``monkeypatch`` a single, well-known attribute
    name without touching the production dependency import.
    """
    return await validate_proxy_api_key(request)


def api_key_validator_with_provider(provider: str):
    """Return a FastAPI dependency that authorizes a key for ``provider``.

    The returned dependency first delegates to the existing API-key
    validator (which handles cache lookup, header parsing, and the
    ``ProxyAuthError`` -> HTTP mapping). If the resolved key's
    ``provider_scope`` does not include ``provider`` the dependency raises
    HTTP 403 with a descriptive detail string.

    ``provider_scope`` is stored as a comma-separated string per the Phase 1
    schema; we split on ``,`` so the legacy ``"codex"`` value, the dual
    ``"codex,claude"`` value, and the new ``"claude"`` value are all
    unambiguous. An empty / missing scope is treated as no authorization
    rather than universal access.
    """

    async def _validate(request: Request) -> Any:
        api_key = await _existing_validate_proxy_api_key(request)
        scope = (getattr(api_key, "provider_scope", "") or "")
        if provider not in scope.split(","):
            raise HTTPException(
                status_code=403,
                detail=f"API key is not authorized for provider '{provider}'",
            )
        return api_key

    return _validate


__all__ = ["api_key_validator_with_provider"]
