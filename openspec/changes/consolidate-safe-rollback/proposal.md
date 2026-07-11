# consolidate-safe-rollback

## Why

PR #5 (`Soju06/fix/async-session-cancel-safety`, merged 2026-01-13) introduced
shielded session rollback/close helpers so a client disconnect mid-request
could not strand a SQLite-pooled connection. It landed the helpers twice — once
in `app/db/session.py` and again at the bottom of
`app/modules/request_logs/repository.py` — and labeled both `_safe_rollback`
with a leading underscore to signal "module private".

Since then a third copy appeared in `app/modules/claude/auth_manager.py`, and
the three copies have drifted:

- `app/db/session.py._safe_rollback` shields via `asyncio.shield` over an
  `ensure_future` task.
- `app/modules/request_logs/repository.py._safe_rollback` shields via
  `anyio.CancelScope(shield=True)`.
- `app/modules/claude/auth_manager.py._safe_rollback` does **not** shield at
  all — a caller cancellation will interrupt the rollback, which is exactly the
  leak PR #5 was trying to prevent, just on a different code path.

The helpers are also imported across module boundaries under their leading
underscore (`from app.db.session import _safe_rollback, _safe_close`), which
silently breaks the next time someone refactors the session module.

This change consolidates the helpers into a single canonical, public
implementation that all call sites share. It is a pure refactor: the externally
observable session cleanup behavior is unchanged on the two paths that already
worked, and it becomes correct on the Claude refresh path that currently does
not shield.

## What Changes

- Add two public, module-level coroutines `safe_rollback(session)` and
  `safe_close(session)` to `app/db/session.py`. These replace the three private
  copies.
- The canonical implementation shields with `asyncio.shield` over an
  `asyncio.ensure_future` task (the proven mechanism in `app/db/session.py`
  today) and catches `BaseException` so the cleanup completes even when the
  caller is cancelled.
- Remove the duplicate `_safe_rollback` definition from
  `app/modules/request_logs/repository.py`; import the canonical helper
  instead.
- Remove the unshielded `_safe_rollback` definition from
  `app/modules/claude/auth_manager.py`; import the canonical helper so the
  Claude refresh path is cancel-safe too.

`app/dependencies.py` does not need an update. A later refactor moved both
context helpers (`_accounts_repo_context`, `_proxy_repo_context`) onto
`get_background_session()`, which encapsulates the cancel-safe cleanup
internally. Verified via `grep -rn "_safe_close\|_safe_rollback" app/`
on `origin/main` — `app/dependencies.py` does not import either symbol.

No public API changes outside the codebase. No database, schema, request, or
response shape changes.

## Capabilities

### Modified Capabilities

- `database-backends`: Add a requirement that the application exposes a single
  canonical set of cancel-safe session cleanup helpers and that all request-
  and background-path session users consume them. The existing requirement
  about detached background tasks owning their session lifetime stays as-is;
  this change is the corollary on the helper that performs the cleanup.

## Impact

- **Backend Python**: Three modules touched — `app/db/session.py`,
  `app/modules/request_logs/repository.py`, `app/modules/claude/auth_manager.py`.
  All changes are import/identifier swaps except for `app/db/session.py`
  where the helpers are renamed and re-exported. `app/dependencies.py` is
  intentionally untouched (see *What Changes* above).
- **Existing tests**: `tests/unit/test_db_session.py` already exercises the
  cancel-safety contract against `session_module._safe_close` /
  `session_module._safe_rollback`. That test must be updated to reference the
  new public names; the contract it asserts (cleanup outlives caller
  cancellation) must continue to hold.
- **Behavior delta**: One — the Claude refresh session cleanup now survives
  caller cancellation. Previously a cancellation during
  `ClaudeAuthManager._run_refresh` could leave a connection checked out of the
  background pool, which over time reproduces the leak PR #5 was fixing for
  the Codex path.
- **No breaking changes** for any HTTP client. No schema, route, or response
  changes.
- **Verification**: `openspec validate consolidate-safe-rollback --strict` must
  pass. `ruff check`, `ruff format --check`, `ty check`, and `pytest` must all
  pass. The existing `_safe_close_outlives_caller_cancellation` and
  `_safe_rollback_outlives_caller_cancellation` tests, retargeted at the new
  public names, must pass.