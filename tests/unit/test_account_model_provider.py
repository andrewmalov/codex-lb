from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime, timezone

import pytest
from sqlalchemy import CheckConstraint, Index
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.models import Account, AccountStatus, Base

pytestmark = pytest.mark.unit


@pytest.fixture
async def async_session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


def test_account_provider_column_is_not_nullable() -> None:
    column = Account.__table__.c.provider
    assert column is not None
    assert column.nullable is False


def test_account_provider_has_check_constraint_constraining_values() -> None:
    table = Account.__table__
    check_constraints = [c for c in table.constraints if isinstance(c, CheckConstraint)]
    provider_checks = [c for c in check_constraints if "provider" in str(c.sqltext).lower()]
    assert provider_checks, "expected a CHECK constraint referencing 'provider'"
    # The constraint name must be stable so the alembic migration can drop it.
    assert any(c.name == "ck_accounts_provider" for c in provider_checks)


def test_account_provider_value_must_be_codex_or_claude(async_session_factory) -> None:
    async def _run() -> None:
        async with async_session_factory() as session:
            account = Account(
                id="acc-bad",
                email="bad@example.com",
                plan_type="plus",
                codex_installation_id="11111111-1111-1111-1111-111111111111",
                access_token_encrypted=b"a",
                refresh_token_encrypted=b"r",
                id_token_encrypted=b"i",
                last_refresh=datetime.now(timezone.utc),
                provider="bogus",  # type: ignore[arg-type]
                status=AccountStatus.ACTIVE,
            )
            session.add(account)
            with pytest.raises(Exception):
                await session.flush()

    asyncio.run(_run())


def test_account_has_claude_columns() -> None:
    names = {c.name for c in Account.__table__.columns}
    expected = {
        "claude_account_uuid",
        "claude_refresh_token_encrypted",
        "claude_access_token_encrypted",
        "claude_access_token_expires_at",
        "claude_scopes",
        "claude_user_email",
        "claude_user_organization_uuid",
        "rate_limit_requests_remaining",
        "rate_limit_requests_reset_at",
        "rate_limit_input_tokens_remaining",
        "rate_limit_input_tokens_reset_at",
        "rate_limit_output_tokens_remaining",
        "rate_limit_output_tokens_reset_at",
        "rate_limit_status",
    }
    missing = expected - names
    assert not missing, f"missing columns: {sorted(missing)}"


def test_claude_account_uuid_partial_unique_index() -> None:
    table = Account.__table__
    indexes = [idx for idx in table.indexes if isinstance(idx, Index)]
    partial = [
        idx
        for idx in indexes
        if idx.unique and any(col.name == "claude_account_uuid" for col in idx.columns)
    ]
    assert partial, "expected a partial unique index on claude_account_uuid"
    idx = partial[0]
    assert idx.name == "uq_accounts_claude_uuid"
    sqlite_where = (idx.dialect_options.get("sqlite") or {}).get("where")
    postgres_where = (idx.dialect_options.get("postgresql") or {}).get("where")
    assert sqlite_where is not None and "claude" in str(sqlite_where).lower()
    assert postgres_where is not None and "claude" in str(postgres_where).lower()


def test_account_email_is_nullable() -> None:
    column = Account.__table__.c.email
    assert column.nullable is True