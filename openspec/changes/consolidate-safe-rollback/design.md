# Design: consolidate-safe-rollback

## Canonical helper

The two helpers live in `app/db/session.py`:

```python
async def safe_rollback(session: AsyncSession) -> None:
    if not session.in_transaction():
        return
    try:
        await _shielded(session.rollback())
    except BaseException:
        return


async def safe_close(session: AsyncSession) -> None:
    try:
        await _shielded(session.close())
    except BaseException:
        return
```

`_shielded` is the existing private wrapper:

```python
async def _shielded(awaitable: Awaitable[object]) -> None:
    task = asyncio.ensure_future(awaitable)
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise
```

Why `asyncio.shield` + `ensure_future` (not `anyio.CancelScope(shield=True)`):

- It is the implementation already in production on the request-path session
  dependency. It is the version exercised by the existing regression tests in
  `tests/unit/test_db_session.py`. Switching shielding primitives is out of
  scope for a consolidation PR and would broaden the surface to re-verify.
- It correctly handles `CancelledError` by awaiting the inner task to
  completion before re-raising. This is the property the contract
  (`safe_*` outlives caller cancellation) depends on.
- It catches `BaseException`, so any cancellation originating inside the
  SQLAlchemy layer is swallowed; only the outer cancellation raised by the
  shield is propagated.

## Call-site consolidation

| File | Old symbol | New symbol |
|------|-----------|------------|
| `app/db/session.py` (internal) | `_safe_rollback`, `_safe_close` | `safe_rollback`, `safe_close` |
| `app/modules/request_logs/repository.py` | local `_safe_rollback` (anyio) | `from app.db.session import safe_rollback` |
| `app/modules/claude/auth_manager.py` | local `_safe_rollback` (no shield) | `from app.db.session import safe_rollback` |
| `app/dependencies.py` | (no direct import — uses `get_background_session()`) | (no change) |

The `safe_close` helper is currently only used inside `app/db/session.py`. We
expose it publicly anyway because (a) the proposal already commits to it,
(b) it is the natural pair of `safe_rollback` and (c) the regression test
already targets it.

## Behavior delta on the Claude refresh path

The single behavior change in this PR is that
`ClaudeAuthManager._refresh_session_context`'s `except BaseException` branch
now invokes the shielded `safe_rollback` instead of an unshielded
`session.rollback()`. Concretely:

- Before: if the caller of `_refresh_session_context` is cancelled while the
  refresh is mid-rollback, the rollback is interrupted, and the background
  engine pool loses one connection per occurrence.
- After: the rollback runs to completion behind `asyncio.shield`. The caller
  cancellation is re-raised after the rollback finishes, so the request-scoped
  error path is unchanged from the caller's perspective.

## Test renames

`tests/unit/test_db_session.py` currently imports the helpers via attribute
access (`session_module._safe_close`, `session_module._safe_rollback`). The
attribute names flip to `safe_close` / `safe_rollback`; the test bodies
themselves do not change. The test names
(`test_safe_close_outlives_caller_cancellation`,
`test_safe_rollback_outlives_caller_cancellation`) already match the public
naming so they stay as-is.

## Out of scope

- Switching to `anyio.CancelScope(shield=True)` everywhere — different
  primitive, different test surface, no observed benefit on this code path.
- Removing the redundant `if session.in_transaction()` guard around
  `safe_rollback` calls in `get_session` / `get_background_session`. The
  guard is redundant (`safe_rollback` already checks) but not incorrect;
  cleaning it up is a separate concern.
- Renaming `_shielded` itself. It remains an implementation detail of
  `safe_rollback` / `safe_close` and is not exported.