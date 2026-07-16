from __future__ import annotations

import asyncio
import contextlib
import importlib
import logging
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Protocol, cast

from app.core.auth.refresh import RefreshError
from app.core.clients.http import refresh_http_client
from app.core.clients.model_fetcher import (
    ModelFetchError,
    fetch_claude_models,
    fetch_models_for_plan,
)
from app.core.config.settings import get_settings
from app.core.crypto import TokenEncryptor
from app.core.openai.model_registry import (
    UpstreamModel,
    _merge_service_tier_metadata,
    get_model_registry,
)
from app.core.upstream_proxy import ResolvedUpstreamRoute, resolve_upstream_route
from app.db.models import Account, AccountStatus
from app.db.session import get_background_session
from app.modules.accounts.auth_manager import AuthManager
from app.modules.accounts.repository import AccountsRepository
from app.modules.proxy.account_cache import get_account_selection_cache

logger = logging.getLogger(__name__)


class _LeaderElectionLike(Protocol):
    async def try_acquire(self) -> bool: ...


class _AuthManagerLike(Protocol):
    """Subset of an auth manager that the model refresh scheduler needs.

    Both :class:`app.modules.accounts.auth_manager.AuthManager` (Codex)
    and the Claude-side adapter (see :class:`_ClaudeAuthManagerAdapter`)
    satisfy this contract: a single ``ensure_fresh`` call that returns a
    refreshed :class:`Account`. Keeping this as a Protocol avoids forcing
    the two concrete classes into a shared inheritance tree they don't
    need to be in.
    """

    async def ensure_fresh(self, account: Account, *, force: bool = False) -> Account: ...


_AuthManagerFactory = Callable[[AccountsRepository], _AuthManagerLike]


@dataclass(slots=True)
class _TransportRecoveryState:
    attempted: bool = False


@dataclass(slots=True)
class _FetchResult:
    models: list[UpstreamModel]
    account_models: dict[str, tuple[str, list[UpstreamModel]]]


def _get_leader_election() -> _LeaderElectionLike:
    module = importlib.import_module("app.core.scheduling.leader_election")
    return cast(_LeaderElectionLike, module.get_leader_election())


def _account_access_token(encryptor: TokenEncryptor, account: Account) -> str:
    """Decrypt and return the access token for ``account``, picking the
    provider-scoped column.

    Claude accounts store the real bearer in
    ``account.claude_access_token_encrypted`` and use the
    NOT-NULL-constrained ``access_token_encrypted`` column as a
    placeholder (``encrypt("claude")``). Reading the Codex column for a
    Claude row decrypts to the literal string ``"claude"`` — sending
    that to any upstream is wrong. For Codex rows the standard column is
    used. See
    ``openspec/changes/fix-model-refresh-scheduler-provider-scope`` and
    its follow-up for the column layout contract.
    """
    if getattr(account, "provider", None) == "claude":
        ciphertext = account.claude_access_token_encrypted
        if ciphertext is None:
            return ""
        return encryptor.decrypt(ciphertext)
    return encryptor.decrypt(account.access_token_encrypted)


def _default_auth_manager_factory(repo: AccountsRepository) -> _AuthManagerLike:
    return AuthManager(repo)


class _ClaudeAuthManagerAdapter:
    """Adapter that exposes :class:`_AuthManagerLike`-shape ``ensure_fresh``
    for Claude OAuth accounts.

    The Codex auth manager reads ``refresh_token_encrypted`` (a placeholder
    for Claude rows; the real refresh token lives in
    ``claude_refresh_token_encrypted``). Invoking the Codex manager for a
    Claude account would either surface a nonsensical refresh failure or
    silently swallow the placeholder. This adapter is a thin Protocol
    shim — Claude OAuth rotation is owned by the dedicated auth guardian
    pass (``app.core.auth.guardian.AuthGuardianScheduler``), so the model
    refresh scheduler does NOT take ownership of rotation here.

    Concretely, ``ensure_fresh`` is a no-op in this adapter. Its sole job
    is to satisfy the scheduler's :class:`_AuthManagerLike` contract so
    the failover loop does not try to instantiate the Codex
    :class:`AuthManager` for Claude accounts. If the access token is
    expired the upstream returns 401, which routes through the scheduler's
    existing 401-retry path (``force=True``); for that branch this
    adapter still defers — rotation is delegated to the auth guardian on
    its next tick. Operators see a model-fetch 401 logged for the account
    rather than a silent ``reauth_required`` flip from a placeholder
    refresh-token decrypt — which is the actual bug the change exists
    to prevent.
    """

    def __init__(self, _repo: AccountsRepository, *, encryptor: TokenEncryptor) -> None:
        # ``_repo`` is kept on the surface for symmetry with the Codex
        # adapter and so tests can introspect the binding, but it is not
        # used by the no-op implementation.
        self._repo = _repo
        self._encryptor = encryptor

    async def ensure_fresh(self, account: Account, *, force: bool = False) -> Account:
        # Rotation is owned by ``app.core.auth.guardian``. Returning
        # the row unchanged keeps the model-discovery loop running; the
        # auth guardian refreshes the credential before its next
        # Claude-touching pass.
        return account


def _build_claude_auth_manager_factory(encryptor: TokenEncryptor) -> _AuthManagerFactory:
    """Build a factory yielding :class:`_ClaudeAuthManagerAdapter` instances
    bound to the scheduler's per-tick ``AccountsRepository``.

    Returning a factory (rather than a singleton adapter) matches the
    Codex path and keeps session ownership at the scheduler boundary.
    """
    def _factory(repo: AccountsRepository) -> _AuthManagerLike:
        return cast(_AuthManagerLike, _ClaudeAuthManagerAdapter(repo, encryptor=encryptor))

    return _factory


@dataclass(slots=True)
class ModelRefreshScheduler:
    interval_seconds: int
    enabled: bool
    _task: asyncio.Task[None] | None = None
    _stop: asyncio.Event = field(default_factory=asyncio.Event)

    async def start(self) -> None:
        if not self.enabled:
            return
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        if not self._task:
            return
        self._stop.set()
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    async def _run_loop(self) -> None:
        while not self._stop.is_set():
            await self._refresh_once()
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval_seconds)
            except asyncio.TimeoutError:
                continue

    async def _refresh_once(self) -> None:
        is_leader = await _get_leader_election().try_acquire()
        if not is_leader:
            return
        try:
            async with get_background_session() as session:
                accounts_repo = AccountsRepository(session)
                encryptor = TokenEncryptor()
                per_plan_results: dict[str, list[UpstreamModel]] = {}
                per_account_results: dict[str, tuple[str, list[UpstreamModel]]] = {}
                active_account_plans: dict[str, str] = {}

                # Provider-scoped fetch — see
                # openspec/changes/fix-model-refresh-scheduler-provider-scope
                # and its follow-up for the bearer/auth-manager scoping.
                # A Claude OAuth bearer MUST NOT be sent to the Codex
                # upstream ``/codex/models`` endpoint (Anthropic returns
                # 401, the scheduler would mark the Claude account
                # ``reauth_required``). The refresh path for Claude
                # accounts MUST use ``ClaudeAuthManager`` so it reads
                # ``claude_refresh_token_encrypted`` (NOT the placeholder
                # Codex-flavored column).
                claude_auth_manager_factory = _build_claude_auth_manager_factory(encryptor)

                provider_candidates_count = 0
                provider_with_results_count = 0

                for provider, fetcher, auth_manager_factory in (
                    ("codex", fetch_models_for_plan, _default_auth_manager_factory),
                    ("claude", fetch_claude_models, claude_auth_manager_factory),
                ):
                    candidates = await accounts_repo.list_accounts_by_provider(provider)
                    if not candidates:
                        continue
                    provider_candidates_count += 1
                    grouped = _group_by_plan(candidates)
                    for plan_type, plan_candidates in grouped.items():
                        for account in plan_candidates:
                            active_account_plans[account.id] = plan_type
                        result = await _fetch_with_failover(
                            plan_candidates,
                            encryptor,
                            accounts_repo,
                            fetcher=fetcher,
                            auth_manager_factory=auth_manager_factory,
                        )
                        if result is not None:
                            per_plan_results[plan_type] = result.models
                            per_account_results.update(result.account_models)
                            provider_with_results_count += 1

                if not per_plan_results:
                    if provider_candidates_count == 0:
                        logger.debug("No active accounts for model registry refresh")
                    else:
                        # Distinct warn: candidates existed but every
                        # provider exhausted retries; surface this so
                        # operators can correlate against upstream
                        # incidents instead of misreading a "no
                        # accounts" quiet day.
                        logger.warning(
                            "Model registry refresh produced no results despite candidates "
                            "providers_with_candidates=%d",
                            provider_candidates_count,
                        )
                    return

                registry = get_model_registry()
                await registry.update(
                    per_plan_results,
                    per_account_results=per_account_results,
                    active_account_plans=active_account_plans,
                )
                snapshot = registry.get_snapshot()
                total_models = len(snapshot.models) if snapshot else 0
                logger.info(
                    "Model registry refreshed plans=%d total_models=%d",
                    len(per_plan_results),
                    total_models,
                )
                get_account_selection_cache().invalidate()
        except Exception:
            logger.exception("Model registry refresh loop failed")


def _group_by_plan(accounts: list[Account]) -> dict[str, list[Account]]:
    grouped: dict[str, list[Account]] = {}
    for account in accounts:
        if account.status != AccountStatus.ACTIVE:
            continue
        plan_type = account.plan_type
        if not plan_type:
            continue
        grouped.setdefault(plan_type, []).append(account)
    return grouped


def _error_summary(exc: BaseException) -> str:
    if isinstance(exc, ModelFetchError):
        summary = f"status={exc.status_code} transport={exc.transport_error}"
        if exc.message:
            summary = f"{summary} message={_compact_error_message(exc.message)}"
        return summary
    if isinstance(exc, RefreshError):
        summary = f"code={exc.code} permanent={exc.is_permanent} transport={exc.transport_error}"
        if exc.message:
            summary = f"{summary} message={_compact_error_message(exc.message)}"
        return summary

    message = _compact_error_message(str(exc))
    if message:
        return f"{exc.__class__.__name__}: {message}"
    return exc.__class__.__name__


def _compact_error_message(message: str) -> str:
    return " ".join(message.split())


async def _fetch_with_failover(
    candidates: list[Account],
    encryptor: TokenEncryptor,
    accounts_repo: AccountsRepository,
    *,
    fetcher: Callable[..., Awaitable[list[UpstreamModel]]] | None = None,
    auth_manager_factory: _AuthManagerFactory | None = None,
) -> _FetchResult | None:
    """Iterate ``candidates`` and call the per-account fetcher.

    ``fetcher`` defaults to :func:`fetch_models_for_plan` (the existing
    Codex upstream path). The model refresh scheduler passes
    :func:`fetch_claude_models` for Claude-provider accounts so a Claude
    bearer is sent to ``{claude_api_base_url}/v1/models``, never to
    ``/codex/models``.

    ``auth_manager_factory`` resolves a provider-scoped auth manager —
    the Codex factory (default) yields :class:`AuthManager`; the Claude
    factory yields a :class:`_ClaudeAuthManagerAdapter`. Mixing a
    Codex auth manager with a Claude account would attempt to refresh
    using the wrong (placeholder) refresh-token column.
    """
    if fetcher is None:
        fetcher = fetch_models_for_plan
    if auth_manager_factory is None:
        auth_manager_factory = _default_auth_manager_factory
    transport_recovery = _TransportRecoveryState()
    successful_results: list[list[UpstreamModel]] = []
    account_models: dict[str, tuple[str, list[UpstreamModel]]] = {}

    for account in candidates:
        auth_manager = auth_manager_factory(accounts_repo)
        try:
            account = await _ensure_fresh_with_transport_recovery(
                auth_manager,
                account,
                transport_recovery=transport_recovery,
            )
            models = await _fetch_models_with_transport_recovery(
                account,
                encryptor,
                transport_recovery=transport_recovery,
                fetcher=fetcher,
            )
            successful_results.append(models)
            account_models[account.id] = (account.plan_type, models)
        except ModelFetchError as exc:
            if exc.status_code == 401:
                try:
                    account = await _ensure_fresh_with_transport_recovery(
                        auth_manager,
                        account,
                        force=True,
                        transport_recovery=transport_recovery,
                    )
                    models = await _fetch_models_with_transport_recovery(
                        account,
                        encryptor,
                        transport_recovery=transport_recovery,
                        fetcher=fetcher,
                    )
                    successful_results.append(models)
                    account_models[account.id] = (account.plan_type, models)
                    continue
                except (ModelFetchError, RefreshError) as retry_exc:
                    logger.warning(
                        "Model fetch auth retry failed account=%s plan=%s initial_error=%s retry_error=%s",
                        account.id,
                        account.plan_type,
                        _error_summary(exc),
                        _error_summary(retry_exc),
                    )
                    continue
            logger.warning(
                "Model fetch failed account=%s plan=%s error=%s",
                account.id,
                account.plan_type,
                _error_summary(exc),
            )
            continue
        except RefreshError as exc:
            logger.warning(
                "Token refresh failed for model fetch account=%s plan=%s error=%s",
                account.id,
                account.plan_type,
                _error_summary(exc),
            )
            continue
        except Exception as exc:
            logger.warning(
                "Unexpected error during model fetch account=%s plan=%s error=%s",
                account.id,
                account.plan_type,
                _error_summary(exc),
                exc_info=True,
            )
            continue
    merged_models = _merge_same_plan_model_results(successful_results)
    if not merged_models:
        return None
    return _FetchResult(models=merged_models, account_models=account_models)


def _merge_same_plan_model_results(successful_results: list[list[UpstreamModel]]) -> list[UpstreamModel]:
    if not successful_results:
        return []

    models_by_slug = [{model.slug: model for model in models} for models in successful_results]
    common_slugs = set(models_by_slug[0])
    for models in models_by_slug[1:]:
        common_slugs.intersection_update(models)

    merged_models: list[UpstreamModel] = []
    for model in models_by_slug[0].values():
        if model.slug not in common_slugs:
            continue
        merged_model = model
        for models in models_by_slug[1:]:
            merged_model = _merge_service_tier_metadata(merged_model, models[model.slug])
        merged_models.append(merged_model)
    return merged_models


async def _ensure_fresh_with_transport_recovery(
    auth_manager: _AuthManagerLike,
    account: Account,
    *,
    transport_recovery: _TransportRecoveryState,
    force: bool = False,
) -> Account:
    try:
        return await auth_manager.ensure_fresh(account, force=force)
    except RefreshError as exc:
        if not exc.transport_error or transport_recovery.attempted:
            raise

        await _refresh_http_client_after_transport_error(account, exc)
        transport_recovery.attempted = True
        return await auth_manager.ensure_fresh(account, force=force)


async def _fetch_models_with_transport_recovery(
    account: Account,
    encryptor: TokenEncryptor,
    *,
    transport_recovery: _TransportRecoveryState,
    fetcher: Callable[..., Awaitable[list[UpstreamModel]]] | None = None,
) -> list[UpstreamModel]:
    """Fetch models for one account, with one transport-error retry.

    ``fetcher`` defaults to :func:`fetch_models_for_plan` (Codex).
    The scheduler passes :func:`fetch_claude_models` for
    ``Account.provider == 'claude'`` rows so the bearer is sent to the
    right upstream — see ``openspec/changes/fix-model-refresh-scheduler-provider-scope``.

    Bearer decryption is provider-scoped via :func:`_account_access_token`
    so the Claude account's real access token is sent to Anthropic (NOT
    the Codex-flavored ``encrypt('claude')`` placeholder). Auth-manager
    selection happens upstream in :func:`_fetch_with_failover`.
    """
    if fetcher is None:
        fetcher = fetch_models_for_plan
    access_token = _account_access_token(encryptor, account)
    fetcher_takes_account_id = fetcher is fetch_models_for_plan
    route = await _resolve_upstream_route_for_account(account, operation="model_discovery")

    try:
        return await _invoke_fetcher(
            fetcher,
            access_token,
            account,
            fetcher_takes_account_id=fetcher_takes_account_id,
            route=route,
        )
    except ModelFetchError as exc:
        if not exc.transport_error or transport_recovery.attempted:
            raise

        await _refresh_http_client_after_transport_error(account, exc)
        transport_recovery.attempted = True
        access_token = _account_access_token(encryptor, account)
        route = await _resolve_upstream_route_for_account(account, operation="model_discovery")
        return await _invoke_fetcher(
            fetcher,
            access_token,
            account,
            fetcher_takes_account_id=fetcher_takes_account_id,
            route=route,
        )


async def _invoke_fetcher(
    fetcher: Callable[..., Awaitable[list[UpstreamModel]]],
    access_token: str,
    account: Account,
    *,
    fetcher_takes_account_id: bool,
    route: ResolvedUpstreamRoute | None,
) -> list[UpstreamModel]:
    """Dispatch a fetcher call with the right per-provider positional shape.

    Codex ``fetch_models_for_plan`` takes ``(access_token, account_id, *, ...)``;
    Anthropic ``fetch_claude_models`` takes ``(access_token, *, ...)``. We
    make the shape explicit (``fetcher_takes_account_id``) instead of
    inspecting signatures: a single boolean survives refactors of either
    fetcher, lets tests pass any callable for either provider, and avoids
    using ``inspect.signature`` (which sees ``(*args, **kwargs)`` for
    ``AsyncMock`` and would mis-classify every stand-in).
    """
    if fetcher_takes_account_id:
        return await fetcher(
            access_token,
            account.chatgpt_account_id,
            route=route,
            allow_direct_egress=route is None,
        )
    return await fetcher(access_token, route=route, allow_direct_egress=route is None)


async def _resolve_upstream_route_for_account(account: Account, *, operation: str) -> ResolvedUpstreamRoute | None:
    async with get_background_session() as session:
        return await resolve_upstream_route(
            session,
            account_id=account.id,
            operation=operation,
            scope="account",
        )


async def _refresh_http_client_after_transport_error(account: Account, transport_exc: BaseException) -> None:
    try:
        await refresh_http_client()
    except Exception as refresh_exc:
        logger.warning(
            "Model fetch transport recovery failed account=%s plan=%s transport_error=%s refresh_error=%s",
            account.id,
            account.plan_type,
            _error_summary(transport_exc),
            _error_summary(refresh_exc),
        )
        raise
    logger.info(
        "Refreshed shared HTTP client after model fetch transport error; retrying account=%s plan=%s error=%s",
        account.id,
        account.plan_type,
        _error_summary(transport_exc),
    )


def build_model_refresh_scheduler() -> ModelRefreshScheduler:
    settings = get_settings()
    return ModelRefreshScheduler(
        interval_seconds=settings.model_registry_refresh_interval_seconds,
        enabled=settings.model_registry_enabled,
    )
