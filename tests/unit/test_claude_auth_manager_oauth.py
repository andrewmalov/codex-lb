"""Tests for ``ClaudeAuthManager.add_claude_account_from_oauth``.

Only the OAuth wrapper is exercised here; the underlying
``add_claude_account`` behavior is exhaustively covered in
``test_claude_account_service.py``.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.modules.claude.auth_manager import (
    ClaudeAccountAlreadyExists,
    ClaudeAuthManager,
)
from app.modules.claude.oauth.tokens import ClaudeOauthClaims

pytestmark = pytest.mark.unit


class _CapturingRepo:
    """In-memory repo stand-in that records the inserted row.

    Implements the full :class:`app.modules.claude.repository.ClaudeAccountRepository`
    protocol as no-ops for methods the OAuth wrapper does not exercise.
    """

    def __init__(self) -> None:
        self.inserted: dict[str, Any] = {}

    async def exists_by_claude_uuid(self, claude_uuid: str) -> bool:
        return False

    async def insert(self, row: dict[str, Any]) -> Any:
        self.inserted = row
        return type("Row", (), {"id": row["id"]})()

    async def get_by_id(self, account_id: str) -> Any:
        return None

    async def update_tokens(self, **_kwargs: Any) -> bool:
        return True

    async def deactivate(self, account_id: str, *, reason: str) -> bool:
        return True

    async def activate(self, account_id: str) -> bool:
        return True

    async def list_accounts(self) -> list[Any]:
        return []

    async def find_due_for_rotation(self, **_kwargs: Any) -> list[Any]:
        return []

    async def count_active(self) -> int:
        return 0


class _ExistingRepo:
    """Repo stand-in that reports the UUID as already-taken.

    Implements the full :class:`app.modules.claude.repository.ClaudeAccountRepository`
    protocol as no-ops for methods the OAuth wrapper does not exercise.
    """

    async def exists_by_claude_uuid(self, claude_uuid: str) -> bool:
        return True

    async def insert(self, row: dict[str, Any]) -> Any:
        return type("Row", (), {"id": row["id"]})()

    async def get_by_id(self, account_id: str) -> Any:
        return None

    async def update_tokens(self, **_kwargs: Any) -> bool:
        return True

    async def deactivate(self, account_id: str, *, reason: str) -> bool:
        return True

    async def activate(self, account_id: str) -> bool:
        return True

    async def list_accounts(self) -> list[Any]:
        return []

    async def find_due_for_rotation(self, **_kwargs: Any) -> list[Any]:
        return []

    async def count_active(self) -> int:
        return 0


@pytest.mark.asyncio
async def test_add_claude_account_from_oauth_delegates_with_claim_derived_fields() -> None:
    repo = _CapturingRepo()
    mgr = ClaudeAuthManager(repo=repo)  # type: ignore[arg-type]
    claims = ClaudeOauthClaims(
        claude_account_uuid="acct-uuid",
        user_email="u@example.test",
        user_organization_uuid="org-uuid",
        scopes=["user:profile", "user:inference"],
    )

    new_id = await mgr.add_claude_account_from_oauth(
        access_token="AT",
        refresh_token="RT",
        expires_in=3600,
        id_token_claims=claims,
    )

    assert new_id == "claude-acct-uuid"
    assert repo.inserted != {}

    row = repo.inserted
    assert row["claude_account_uuid"] == "acct-uuid"
    assert row["claude_user_email"] == "u@example.test"
    assert row["claude_user_organization_uuid"] == "org-uuid"
    assert json.loads(row["claude_scopes"]) == ["user:profile", "user:inference"]


@pytest.mark.asyncio
async def test_add_claude_account_from_oauth_propagates_duplicate() -> None:
    mgr = ClaudeAuthManager(repo=_ExistingRepo())  # type: ignore[arg-type]
    claims = ClaudeOauthClaims(
        claude_account_uuid="duplicate",
        user_email=None,
        user_organization_uuid=None,
        scopes=None,
    )

    with pytest.raises(ClaudeAccountAlreadyExists):
        await mgr.add_claude_account_from_oauth(
            access_token="AT",
            refresh_token="RT",
            expires_in=3600,
            id_token_claims=claims,
        )


@pytest.mark.asyncio
async def test_tokens_are_encrypted_in_storage() -> None:
    repo = _CapturingRepo()
    mgr = ClaudeAuthManager(repo=repo)  # type: ignore[arg-type]
    claims = ClaudeOauthClaims(claude_account_uuid="acct-uuid")

    await mgr.add_claude_account_from_oauth(
        access_token="PLAINTEXT_AT",
        refresh_token="PLAINTEXT_RT",
        expires_in=3600,
        id_token_claims=claims,
    )

    # Encrypted tokens are stored as bytes (Fernet envelope) — must not
    # contain plaintext.
    assert b"PLAINTEXT_AT" not in repo.inserted["claude_access_token_encrypted"]
    assert b"PLAINTEXT_RT" not in repo.inserted["claude_refresh_token_encrypted"]
