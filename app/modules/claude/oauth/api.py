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

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth.dependencies import (
    require_dashboard_write_access,
    set_dashboard_error_format,
    validate_dashboard_session,
)
from app.db.session import get_session
from app.modules.claude.auth_manager import ClaudeAuthManager
from app.modules.claude.oauth.schemas import (
    ClaudeOauthCallbackRequest,
    ClaudeOauthCallbackResponse,
    ClaudeOauthStartResponse,
    ClaudeOauthStatusResponse,
)
from app.modules.claude.oauth.service import ClaudeOAuthService, ClaudeOauthFlowError
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
    session: AsyncSession = Depends(get_session),
) -> AsyncIterator[ClaudeOAuthService]:
    """Build the default service for a request.

    Tasks 7 wires the lifespan-provided ``ClaudeOAuthClient`` (via a real
    transport). For this task we use a placeholder transport that surfaces
    only what the service needs; tests monkeypatch this dependency.
    """
    from app.core.clients.anthropic.oauth import ClaudeOAuthClient
    from app.core.config.settings import get_settings

    settings = get_settings()
    repo = SqlClaudeAccountRepository(session)
    # NOTE: the lifespan-provided real ``ClaudeOAuthClient`` is wired in Task 7.
    # For this task the dependency seam is intentionally a stub so tests can
    # monkeypatch ``get_claude_oauth_service`` cleanly. Production must
    # replace this with the real client from ``app.state.claude_oauth_client``
    # (see Task 7 in the implementation plan).
    class _PlaceholderTransport:
        async def post(self, url: str, *, json: dict, headers: dict):  # pragma: no cover - unreachable in task 5
            raise NotImplementedError("claude_oauth_client must be wired in app.state (Task 7)")

    client = ClaudeOAuthClient(transport=_PlaceholderTransport(), settings=settings)
    manager = ClaudeAuthManager(repo=repo)
    yield ClaudeOAuthService(
        settings=settings,
        oauth_client=client,
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