from __future__ import annotations

import contextlib
import logging
from datetime import datetime
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

import app.core.auth.refresh as refresh_module
import app.core.clients.model_fetcher as model_fetcher_module
import app.core.openai.model_refresh_scheduler as scheduler_module
from app.core.crypto import TokenEncryptor
from app.core.openai.model_registry import ReasoningLevel, UpstreamModel
from app.core.upstream_proxy import ResolvedProxyEndpoint, ResolvedUpstreamRoute
from app.db.models import Account, AccountStatus

pytestmark = pytest.mark.unit


def _account(account_id: str = "account-1") -> Account:
    encryptor = TokenEncryptor()
    return Account(
        id=account_id,
        email=f"{account_id}@example.test",
        plan_type="team",
        chatgpt_account_id=f"chatgpt-{account_id}",
        access_token_encrypted=encryptor.encrypt("sk-ant-oat01-AT"),
        refresh_token_encrypted=encryptor.encrypt("sk-ant-refresh-token"),
        id_token_encrypted=encryptor.encrypt("id-token"),
        last_refresh=datetime(2026, 1, 1),
        status=AccountStatus.ACTIVE,
    )


def _model(slug: str) -> UpstreamModel:
    return UpstreamModel(
        slug=slug,
        display_name=slug,
        description=f"Model {slug}",
        context_window=128000,
        input_modalities=("text",),
        supported_reasoning_levels=(ReasoningLevel(effort="medium", description="balanced"),),
        default_reasoning_level="medium",
        supports_reasoning_summaries=False,
        support_verbosity=False,
        default_verbosity=None,
        prefer_websockets=False,
        supports_parallel_tool_calls=True,
        supported_in_api=True,
        minimal_client_version=None,
        priority=0,
        available_in_plans=frozenset(),
        raw={},
    )


class _StubAuthManager:
    def __init__(self, _repo: object) -> None:
        pass

    async def ensure_fresh(self, account: Account, *, force: bool = False) -> Account:
        return account


def _route() -> ResolvedUpstreamRoute:
    return ResolvedUpstreamRoute(
        mode="account_bound",
        pool_id="pool_1",
        endpoint=ResolvedProxyEndpoint("ep_1", "http", "proxy.test", 8080),
    )


@pytest.mark.asyncio
async def test_fetch_models_for_plan_marks_transport_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    session = MagicMock()
    session.get.side_effect = aiohttp.ClientError("dns failed")

    monkeypatch.setattr(
        model_fetcher_module,
        "get_codex_version_cache",
        lambda: SimpleNamespace(get_version=AsyncMock(return_value="1.2.3")),
    )

    @contextlib.asynccontextmanager
    async def lease_session():
        yield session

    monkeypatch.setattr(model_fetcher_module, "lease_http_session", lease_session)
    monkeypatch.setattr(
        model_fetcher_module,
        "get_settings",
        lambda: SimpleNamespace(upstream_base_url="https://example.test/backend-api"),
    )

    with pytest.raises(model_fetcher_module.ModelFetchError) as excinfo:
        await model_fetcher_module.fetch_models_for_plan("access-token", "account-1", allow_direct_egress=True)

    exc = excinfo.value
    assert exc.status_code == 0
    assert exc.transport_error is True
    assert "dns failed" in exc.message


@pytest.mark.asyncio
async def test_refresh_access_token_marks_transport_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    session = MagicMock()
    session.post.side_effect = aiohttp.ClientError("dns failed")

    monkeypatch.setattr(
        refresh_module,
        "get_settings",
        lambda: SimpleNamespace(
            auth_base_url="https://auth.example.test",
            oauth_client_id="client-id",
            oauth_scope="openid profile",
            token_refresh_timeout_seconds=15.0,
        ),
    )

    with pytest.raises(refresh_module.RefreshError) as excinfo:
        await refresh_module.refresh_access_token("refresh-token", session=session, allow_direct_egress=True)

    exc = excinfo.value
    assert exc.code == "transport_error"
    assert exc.is_permanent is False
    assert exc.transport_error is True
    assert "dns failed" in exc.message


@pytest.mark.asyncio
async def test_fetch_with_failover_refreshes_http_client_after_transport_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = _account()
    encryptor = MagicMock()
    encryptor.decrypt.return_value = "access-token"
    expected_models = [_model("gpt-5.4")]

    fetch_models_for_plan = AsyncMock(
        side_effect=[
            scheduler_module.ModelFetchError(0, "temporary dns failure", transport_error=True),
            expected_models,
        ]
    )
    refresh_http_client = AsyncMock()

    monkeypatch.setattr(scheduler_module, "AuthManager", _StubAuthManager)
    monkeypatch.setattr(scheduler_module, "fetch_models_for_plan", fetch_models_for_plan)
    monkeypatch.setattr(scheduler_module, "refresh_http_client", refresh_http_client)

    result = await scheduler_module._fetch_with_failover([account], encryptor, MagicMock())

    assert result is not None
    assert result.models == expected_models
    assert result.account_models == {account.id: (account.plan_type, expected_models)}
    refresh_http_client.assert_awaited_once()
    assert fetch_models_for_plan.await_count == 2
    assert encryptor.decrypt.call_count == 2


@pytest.mark.asyncio
async def test_fetch_models_with_transport_recovery_passes_resolved_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = _account()
    encryptor = MagicMock()
    encryptor.decrypt.return_value = "access-token"
    route = _route()
    expected_models = [_model("gpt-5.4")]
    fetch_models_for_plan = AsyncMock(return_value=expected_models)
    resolve_upstream_route = AsyncMock(return_value=route)

    monkeypatch.setattr(scheduler_module, "fetch_models_for_plan", fetch_models_for_plan)
    monkeypatch.setattr(scheduler_module, "resolve_upstream_route", resolve_upstream_route)

    result = await scheduler_module._fetch_models_with_transport_recovery(
        account,
        encryptor,
        transport_recovery=scheduler_module._TransportRecoveryState(),
    )

    assert result == expected_models
    fetch_models_for_plan.assert_awaited_once_with(
        "access-token",
        "chatgpt-account-1",
        route=route,
        allow_direct_egress=False,
    )
    assert resolve_upstream_route.await_args is not None
    assert resolve_upstream_route.await_args.kwargs["account_id"] == "account-1"


@pytest.mark.asyncio
async def test_fetch_with_failover_refreshes_http_client_after_token_refresh_transport_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = _account()
    encryptor = MagicMock()
    encryptor.decrypt.return_value = "access-token"
    expected_models = [_model("gpt-5.4")]
    ensure_fresh_calls = 0

    class TransportFailingAuthManager:
        def __init__(self, _repo: object) -> None:
            pass

        async def ensure_fresh(self, account: Account, *, force: bool = False) -> Account:
            nonlocal ensure_fresh_calls
            ensure_fresh_calls += 1
            if ensure_fresh_calls == 1:
                raise scheduler_module.RefreshError(
                    "transport_error",
                    "Transport error during token refresh: dns failed",
                    False,
                    transport_error=True,
                )
            return account

    fetch_models_for_plan = AsyncMock(return_value=expected_models)
    refresh_http_client = AsyncMock()

    monkeypatch.setattr(scheduler_module, "AuthManager", TransportFailingAuthManager)
    monkeypatch.setattr(scheduler_module, "fetch_models_for_plan", fetch_models_for_plan)
    monkeypatch.setattr(scheduler_module, "refresh_http_client", refresh_http_client)

    result = await scheduler_module._fetch_with_failover([account], encryptor, MagicMock())

    assert result is not None
    assert result.models == expected_models
    assert result.account_models == {account.id: (account.plan_type, expected_models)}
    refresh_http_client.assert_awaited_once()
    assert ensure_fresh_calls == 2
    fetch_models_for_plan.assert_awaited_once()


@pytest.mark.asyncio
async def test_fetch_with_failover_attempts_transport_recovery_once_when_retry_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    accounts = [_account("account-1"), _account("account-2")]
    encryptor = MagicMock()
    encryptor.decrypt.return_value = "access-token"

    fetch_models_for_plan = AsyncMock(
        side_effect=[
            scheduler_module.ModelFetchError(0, "temporary dns failure", transport_error=True),
            scheduler_module.ModelFetchError(0, "temporary dns failure", transport_error=True),
            scheduler_module.ModelFetchError(0, "temporary dns failure", transport_error=True),
        ]
    )
    refresh_http_client = AsyncMock()

    monkeypatch.setattr(scheduler_module, "AuthManager", _StubAuthManager)
    monkeypatch.setattr(scheduler_module, "fetch_models_for_plan", fetch_models_for_plan)
    monkeypatch.setattr(scheduler_module, "refresh_http_client", refresh_http_client)

    result = await scheduler_module._fetch_with_failover(accounts, encryptor, MagicMock())

    assert result is None
    refresh_http_client.assert_awaited_once()
    assert fetch_models_for_plan.await_count == 3
    assert encryptor.decrypt.call_count == 3


@pytest.mark.asyncio
async def test_fetch_with_failover_unions_same_plan_tiers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    accounts = [_account("account-1"), _account("account-2")]
    encryptor = MagicMock()
    encryptor.decrypt.return_value = "access-token"
    first_models = [_model("gpt-5.4")]
    first_models[0].raw["service_tiers"] = [{"slug": "default"}]
    second_models = [_model("gpt-5.4")]
    second_models[0].raw["service_tiers"] = [{"slug": "fast"}]
    second_models[0].raw["additional_speed_tiers"] = ["fast"]

    fetch_models_for_plan = AsyncMock(side_effect=[first_models, second_models])

    monkeypatch.setattr(scheduler_module, "AuthManager", _StubAuthManager)
    monkeypatch.setattr(scheduler_module, "fetch_models_for_plan", fetch_models_for_plan)

    result = await scheduler_module._fetch_with_failover(accounts, encryptor, MagicMock())

    assert result is not None
    assert [model.slug for model in result.models] == ["gpt-5.4"]
    assert result.account_models == {
        accounts[0].id: (accounts[0].plan_type, first_models),
        accounts[1].id: (accounts[1].plan_type, second_models),
    }
    service_tiers = result.models[0].raw["service_tiers"]
    assert isinstance(service_tiers, list)
    assert {tier.get("slug") for tier in service_tiers if isinstance(tier, dict)} == {"default", "fast"}
    assert result.models[0].raw["additional_speed_tiers"] == ["fast"]
    assert fetch_models_for_plan.await_count == 2
    assert [call.args[1] for call in fetch_models_for_plan.await_args_list] == [
        "chatgpt-account-1",
        "chatgpt-account-2",
    ]


@pytest.mark.asyncio
async def test_fetch_with_failover_excludes_same_plan_private_model_slug(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    accounts = [_account("account-1"), _account("account-2")]
    encryptor = MagicMock()
    encryptor.decrypt.return_value = "access-token"
    first_models = [_model("gpt-5.4"), _model("private-alpha")]
    second_models = [_model("gpt-5.4")]

    fetch_models_for_plan = AsyncMock(side_effect=[first_models, second_models])

    monkeypatch.setattr(scheduler_module, "AuthManager", _StubAuthManager)
    monkeypatch.setattr(scheduler_module, "fetch_models_for_plan", fetch_models_for_plan)

    result = await scheduler_module._fetch_with_failover(accounts, encryptor, MagicMock())

    assert result is not None
    assert [model.slug for model in result.models] == ["gpt-5.4"]
    assert fetch_models_for_plan.await_count == 2


@pytest.mark.asyncio
async def test_fetch_with_failover_does_not_warn_after_successful_auth_retry(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    account = _account()
    encryptor = MagicMock()
    encryptor.decrypt.return_value = "access-token"
    expected_models = [_model("gpt-5.4")]

    fetch_models_for_plan = AsyncMock(
        side_effect=[
            scheduler_module.ModelFetchError(401, "expired token"),
            expected_models,
        ]
    )

    monkeypatch.setattr(scheduler_module, "AuthManager", _StubAuthManager)
    monkeypatch.setattr(scheduler_module, "fetch_models_for_plan", fetch_models_for_plan)

    with caplog.at_level(logging.WARNING, logger=scheduler_module.logger.name):
        result = await scheduler_module._fetch_with_failover([account], encryptor, MagicMock())

    assert result is not None
    assert result.models == expected_models
    assert result.account_models == {account.id: (account.plan_type, expected_models)}
    assert fetch_models_for_plan.await_count == 2
    assert "Model fetch failed" not in caplog.text


# ---------------------------------------------------------------------------
# Provider scope — openspec/changes/fix-model-refresh-scheduler-provider-scope
# ---------------------------------------------------------------------------


def _claude_account(account_id: str = "claude-account-1") -> Account:
    """Build a Claude OAuth account row that mirrors the production layout.

    ``access_token_encrypted`` carries the real ``encrypt("claude")``
    placeholder ciphertext the production code-path writes into the
    NOT-NULL Codex-flavored column
    (``app.modules.claude.auth_manager.add_claude_account``,
    line ~279). ``claude_access_token_encrypted`` carries the real bearer
    ciphertext — every Claude row's actual access token. Tests must
    keep these two ciphertexts distinct so the
    :func:`_account_access_token` resolver visibly picks the right
    column. See
    ``openspec/changes/fix-model-refresh-scheduler-provider-scope`` and
    its follow-up for the column layout contract.
    """
    encryptor = TokenEncryptor()
    return Account(
        id=account_id,
        email=f"{account_id}@example.test",
        plan_type="claude_subscription",
        provider="claude",
        chatgpt_account_id=None,
        claude_account_uuid=f"claude-uuid-{account_id}",
        claude_user_email=f"{account_id}@example.test",
        claude_user_organization_uuid=None,
        access_token_encrypted=encryptor.encrypt("claude"),
        refresh_token_encrypted=encryptor.encrypt("claude"),
        id_token_encrypted=encryptor.encrypt("claude"),
        claude_access_token_encrypted=encryptor.encrypt("sk-ant-real-bearer"),
        claude_refresh_token_encrypted=encryptor.encrypt("sk-ant-real-refresh"),
        last_refresh=datetime(2026, 1, 1),
        status=AccountStatus.ACTIVE,
    )


async def test_fetch_with_failover_uses_injected_fetcher(monkeypatch: pytest.MonkeyPatch) -> None:
    """The fetcher kwarg on `_fetch_with_failover` MUST be honored —
    this is the hook that routes Claude accounts to
    ``fetch_claude_models`` and away from the Codex upstream.

    The encryptor is the real :class:`TokenEncryptor` (not a mock) so
    the test catches the provider-scoped bearer-source bug too: the
    Claude account's ``claude_access_token_encrypted`` ("sk-ant-real-bearer")
    MUST be the value handed to ``fetch_claude_models`` — NOT the
    Codex-flavored ``encrypt("claude")`` placeholder in
    ``access_token_encrypted``. The two columns are encrypted with
    distinct material via :func:`_claude_account`; a regression that
    decrypts the wrong column would surface here as ``sk-ant-real-bearer``
    never reaching the fetcher and the test failing on the
    ``access_token`` assertion below.
    """
    claude_account = _claude_account("claude-1")
    claude_models = [_model("claude-opus-4-20250514")]

    codex_fetcher = AsyncMock(
        side_effect=AssertionError("Codex fetcher MUST NOT be called for Claude accounts")
    )
    claude_fetcher = AsyncMock(return_value=claude_models)

    monkeypatch.setattr(scheduler_module, "AuthManager", _StubAuthManager)
    monkeypatch.setattr(scheduler_module, "fetch_models_for_plan", codex_fetcher)

    encryptor = TokenEncryptor()

    result = await scheduler_module._fetch_with_failover(
        [claude_account],
        encryptor,
        MagicMock(),
        fetcher=claude_fetcher,
    )

    assert result is not None
    assert [m.slug for m in result.models] == ["claude-opus-4-20250514"]
    assert result.account_models == {claude_account.id: ("claude_subscription", claude_models)}
    claude_fetcher.assert_awaited_once()
    codex_fetcher.assert_not_called()
    # The Claude fetcher MUST receive the real Claude bearer
    # (``sk-ant-real-bearer``); a bug that decrypts the Codex-flavored
    # placeholder column would surface as ``"claude"`` here.
    claude_args, _ = claude_fetcher.await_args
    assert claude_args == ("sk-ant-real-bearer",)


async def test_fetch_with_failover_uses_claude_auth_manager_for_claude_accounts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression for Blocker B: the Claude branch of ``_fetch_with_failover``
    MUST instantiate a Claude-scoped auth manager — NOT the Codex one.
    Invoking :class:`AuthManager` on a Claude account would attempt to
    read ``refresh_token_encrypted`` (the placeholder column) and either
    silently swallow or surface a nonsensical refresh error.

    The model's "Claude auth manager" here is a thin Protocol-conforming
    adapter (``_ClaudeAuthManagerAdapter``) that defers rotation to the
    dedicated auth guardian pass — see the adapter's docstring. This
    test pins the wiring only: the failover loop must route Claude
    accounts through the Claude adapter factory.
    """
    claude_account = _claude_account("claude-1")
    claude_models = [_model("claude-opus-4-20250514")]
    claude_fetcher = AsyncMock(return_value=claude_models)

    codex_constructor = MagicMock(
        side_effect=AssertionError(
            "Codex AuthManager MUST NOT be constructed for Claude accounts"
        )
    )
    monkeypatch.setattr(scheduler_module, "AuthManager", codex_constructor)
    monkeypatch.setattr(scheduler_module, "fetch_models_for_plan", codex_constructor)

    fake_repo = MagicMock()
    fake_repo.session = MagicMock(refresh=AsyncMock())

    claude_factory = scheduler_module._build_claude_auth_manager_factory(TokenEncryptor())
    result = await scheduler_module._fetch_with_failover(
        [claude_account],
        TokenEncryptor(),
        cast(Any, fake_repo),
        fetcher=claude_fetcher,
        auth_manager_factory=claude_factory,
    )

    assert result is not None
    codex_constructor.assert_not_called()
    claude_fetcher.assert_awaited_once()


def test_account_access_token_picks_claude_column_for_claude_rows() -> None:
    """Regression for Blocker A: the bearer sent to ``fetch_claude_models``
    MUST be ``encryptor.decrypt(account.claude_access_token_encrypted)``,
    never the Codex-flavored ``account.access_token_encrypted`` (which is
    the ``encrypt("claude")`` placeholder in production).
    """
    claude_account = _claude_account("claude-1")
    encryptor = TokenEncryptor()

    bearer = scheduler_module._account_access_token(encryptor, claude_account)

    assert bearer == "sk-ant-real-bearer"
    # Decrypting the Codex-flavored column directly MUST surface the
    # literal placeholder — this proves the resolver picked the right
    # column above.
    assert encryptor.decrypt(claude_account.access_token_encrypted) == "claude"


async def test_refresh_once_calls_only_codex_fetcher_for_codex_accounts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: ``_refresh_once`` MUST iterate by provider — Codex accounts
    go through ``fetch_models_for_plan``, Claude accounts through
    ``fetch_claude_models``. A cross-provider leak (Codex fetcher called
    with Claude bearer) was the 2026-07-15 incident.
    """
    codex_account = _account("codex-1")
    codex_account.provider = "codex"
    claude_account = _claude_account("claude-1")

    codex_fetcher = AsyncMock(return_value=[_model("gpt-5.4")])
    claude_fetcher = AsyncMock(return_value=[_model("claude-opus-4-20250514")])

    registry = MagicMock()
    registry.update = AsyncMock()

    def _list_by_provider(provider: str) -> list[Account]:
        if provider == "codex":
            return [codex_account]
        if provider == "claude":
            return [claude_account]
        return []

    class _FakeRepo:
        def __init__(self, _session: object) -> None:
            self.list_accounts_by_provider = AsyncMock(side_effect=_list_by_provider)
            # ``_refresh_once`` does ``session.refresh(account)`` after
            # every Claude rotation pass; the refresher may decide a
            # Claude account is not yet due and skip the call entirely,
            # so this is best-effort — both branches are exercised.
            self.session = MagicMock(refresh=AsyncMock())

    monkeypatch.setattr(scheduler_module, "AccountsRepository", _FakeRepo)
    monkeypatch.setattr(scheduler_module, "AuthManager", _StubAuthManager)
    monkeypatch.setattr(scheduler_module, "fetch_models_for_plan", codex_fetcher)
    monkeypatch.setattr(scheduler_module, "fetch_claude_models", claude_fetcher)
    monkeypatch.setattr(scheduler_module, "get_model_registry", lambda: registry)

    @contextlib.asynccontextmanager
    async def session_ctx():
        yield MagicMock()

    monkeypatch.setattr(scheduler_module, "get_background_session", session_ctx)
    monkeypatch.setattr(scheduler_module, "TokenEncryptor", TokenEncryptor)
    monkeypatch.setattr(scheduler_module, "_get_leader_election", lambda: _AlwaysLeader())
    monkeypatch.setattr(scheduler_module, "_resolve_upstream_route_for_account", AsyncMock(return_value=None))

    scheduler = scheduler_module.ModelRefreshScheduler(interval_seconds=3600, enabled=True)
    await scheduler._refresh_once()

    codex_fetcher.assert_awaited_once()
    claude_fetcher.assert_awaited_once()
    # Each fetcher was called with its own account only.
    codex_call_args, codex_call_kwargs = codex_fetcher.await_args
    assert codex_call_args == ("sk-ant-oat01-AT", codex_account.chatgpt_account_id)
    # The Codex dispatcher passes ``allow_direct_egress=True`` when no
    # upstream route resolves (the test fixture returns ``None``).
    assert codex_call_kwargs.get("allow_direct_egress") is True
    # The Claude fetcher receives ONLY a positional ``access_token``;
    # ``account_id`` was dropped from ``fetch_claude_models`` because
    # Anthropic rejects ``chatgpt-account-id`` headers outright.
    claude_call_args, _claude_call_kwargs = claude_fetcher.await_args
    assert claude_call_args == ("sk-ant-real-bearer",)
    # Codex fetcher MUST NOT have seen the Claude bearer.
    assert "sk-ant-real-bearer" not in codex_call_args
    assert "sk-ant-oat01-AT" not in claude_call_args

    registry.update.assert_awaited_once()
    call = registry.update.await_args
    args = call.args
    kwargs = call.kwargs
    per_plan_results = kwargs.get("per_plan_results") if "per_plan_results" in kwargs else (args[0] if args else None)
    active_account_plans = kwargs.get("active_account_plans") if "active_account_plans" in kwargs else (
        args[1] if len(args) > 1 else None
    )
    assert per_plan_results is not None, f"registry.update called with args={args} kwargs={kwargs}"
    assert "gpt-5.4" in per_plan_results["team"][0].slug
    assert "claude-opus-4-20250514" in per_plan_results["claude_subscription"][0].slug
    assert active_account_plans[codex_account.id] == "team"
    assert active_account_plans[claude_account.id] == "claude_subscription"


async def test_refresh_once_skips_provider_with_no_accounts(monkeypatch: pytest.MonkeyPatch) -> None:
    """If a provider has no accounts, its fetcher MUST NOT be called —
    no surprise network traffic for empty partitions.
    """
    codex_fetcher = AsyncMock(
        side_effect=AssertionError("Codex fetcher MUST NOT be called when there are no accounts")
    )
    claude_fetcher = AsyncMock(
        side_effect=AssertionError("Claude fetcher MUST NOT be called when there are no accounts")
    )

    class _FakeRepoEmpty:
        def __init__(self, _session: object) -> None:
            self.list_accounts_by_provider = AsyncMock(return_value=[])
            self.session = MagicMock(refresh=AsyncMock())

    monkeypatch.setattr(scheduler_module, "AccountsRepository", _FakeRepoEmpty)
    monkeypatch.setattr(scheduler_module, "AuthManager", _StubAuthManager)
    monkeypatch.setattr(scheduler_module, "fetch_models_for_plan", codex_fetcher)
    monkeypatch.setattr(scheduler_module, "fetch_claude_models", claude_fetcher)

    @contextlib.asynccontextmanager
    async def session_ctx():
        yield MagicMock()

    monkeypatch.setattr(scheduler_module, "get_background_session", session_ctx)
    monkeypatch.setattr(scheduler_module, "TokenEncryptor", lambda: MagicMock())
    monkeypatch.setattr(scheduler_module, "_get_leader_election", lambda: _AlwaysLeader())

    scheduler = scheduler_module.ModelRefreshScheduler(interval_seconds=3600, enabled=True)
    await scheduler._refresh_once()

    codex_fetcher.assert_not_called()
    claude_fetcher.assert_not_called()


class _AlwaysLeader:
    async def try_acquire(self) -> bool:
        return True
