"""FastAPI router for the Claude OAuth link flow endpoints.

Three endpoints under ``/api/claude/oauth``:

- ``POST /api/claude/oauth/start``   — start a flow (write access required).
- ``GET  /api/claude/oauth/status``  — read flow status.
- ``POST /api/claude/oauth/callback`` — submit pasted code (write access required).

The business logic lives in :class:`ClaudeOAuthService`. This module is a
thin HTTP envelope: dashboard auth + write-access dependency injection,
status-code mapping for :class:`ClaudeOauthFlowError`, request/response
shape serialization.
"""

from __future__ import annotations

import logging
from typing import Any, AsyncIterator

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth.dependencies import (
    require_dashboard_write_access,
    set_dashboard_error_format,
    validate_dashboard_session,
)
from app.core.config.settings import get_settings
from app.db.session import get_session
from app.modules.claude.auth_manager import ClaudeAuthManager
from app.modules.claude.oauth.schemas import (
    ClaudeOauthCallbackRequest,
    ClaudeOauthCallbackResponse,
    ClaudeOauthStartResponse,
    ClaudeOauthStatusResponse,
)
from app.modules.claude.oauth.service import ClaudeOauthFlowError, ClaudeOAuthService
from app.modules.claude.repository import SqlClaudeAccountRepository

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/claude/oauth",
    tags=["dashboard-claude-oauth"],
    dependencies=[Depends(validate_dashboard_session), Depends(set_dashboard_error_format)],
)


# ---------------------------------------------------------------------------
# Dependency override seam
# ---------------------------------------------------------------------------


async def get_claude_oauth_service(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> AsyncIterator[ClaudeOAuthService]:
    """Build the default service for a request.

    Reuses the lifespan-provided :class:`ClaudeOAuthClient` (built by
    :func:`app.modules.claude.wiring.build_claude_oauth_client`) and the
    request-scoped ``SqlClaudeAccountRepository`` so callback persistence
    commits within the request transaction.

    Raises :class:`RuntimeError` when the lifespan has not configured the
    client — that is a wiring bug, not a runtime fallback. Tests override
    this dependency directly via ``app.dependency_overrides``.
    """
    settings = get_settings()
    repo = SqlClaudeAccountRepository(session)

    oauth_client = getattr(request.app.state, "claude_oauth_client", None)
    if oauth_client is None:
        # Lifespan did not provide a client. Fail loudly so this is fixed in
        # tests / production setup, not silently fall back to a placeholder.
        raise RuntimeError(
            "claude_oauth_client is not configured on app.state. "
            "Ensure app_lifespan creates a ClaudeOAuthClient "
            "(see app/modules/claude/wiring.py::build_claude_oauth_client)."
        )

    manager = ClaudeAuthManager(repo=repo)
    yield ClaudeOAuthService(
        settings=settings,
        oauth_client=oauth_client,
        auth_manager=manager,
        accounts_repo=repo,
    )


# ---------------------------------------------------------------------------
# Error code → HTTP status
# ---------------------------------------------------------------------------


_ERROR_CODE_TO_HTTP: dict[str, int] = {
    "flow_not_found": 404,
    "flow_expired": 410,
    "flow_not_pending": 409,
    "state_mismatch": 400,
    "missing_code": 400,
    "invalid_grant": 502,
    "anthropic_unreachable": 502,
    "id_token_missing": 400,
    "id_token_malformed": 400,
    "id_token_claims_incomplete": 400,
    "account_already_exists": 409,
}


def _error_envelope(code: str, message: str) -> dict[str, Any]:
    """Match the project's standard dashboard-error envelope."""
    return {"error": {"code": code, "message": message}}


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/start", response_model=ClaudeOauthStartResponse)
async def start_oauth(
    _write=Depends(require_dashboard_write_access),
    service: ClaudeOAuthService = Depends(get_claude_oauth_service),
) -> ClaudeOauthStartResponse:
    return await service.start_oauth()


@router.get("/status", response_model=ClaudeOauthStatusResponse)
async def oauth_status(
    flowId: str = Query(..., alias="flowId"),
    service: ClaudeOAuthService = Depends(get_claude_oauth_service),
) -> ClaudeOauthStatusResponse:
    return await service.oauth_status(flowId)


@router.post("/callback", response_model=ClaudeOauthCallbackResponse)
async def callback_oauth(
    payload: ClaudeOauthCallbackRequest,
    _write=Depends(require_dashboard_write_access),
    service: ClaudeOAuthService = Depends(get_claude_oauth_service),
) -> ClaudeOauthCallbackResponse:
    try:
        return await service.complete_oauth(
            flow_id=payload.flow_id,
            code=payload.code,
            state=payload.state,
        )
    except ClaudeOauthFlowError as exc:
        from fastapi import HTTPException

        status = _ERROR_CODE_TO_HTTP.get(exc.code, 400)
        raise HTTPException(
            status_code=status,
            detail=_error_envelope(exc.code, str(exc)),
        ) from exc