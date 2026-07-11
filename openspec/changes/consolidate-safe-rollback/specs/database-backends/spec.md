# Spec Delta: database-backends

## ADDED Requirements

### Requirement: Application exposes a single canonical set of cancel-safe session cleanup helpers

The application MUST expose `safe_rollback(session)` and `safe_close(session)`
as public coroutines on `app.db.session` (no leading underscore) and every
request-path or background-path session cleanup site MUST call them rather
than re-implementing the cancel-safety dance locally.

Each helper MUST satisfy the following contract:

- `safe_rollback(session)` MUST be a no-op when `session.in_transaction()`
  is `False`.
- When the session has an open transaction, the helper MUST shield the
  `session.rollback()` awaitable so that a cancellation arriving at the caller
  does not interrupt the rollback. The shielded inner task MUST be awaited to
  completion before the helper returns.
- `safe_close(session)` MUST shield the `session.close()` awaitable with the
  same semantics as `safe_rollback`.
- Both helpers MUST catch `BaseException` from the shielded inner task (not
  just `Exception`) and swallow it. The cancellation that triggered the
  cleanup is allowed to propagate to the caller of the helper only via the
  outer `await` — never as a swallowed side effect.

#### Scenario: Claude refresh session cleanup survives caller cancellation

- **GIVEN** `ClaudeAuthManager._run_refresh` holds a `get_background_session()`
  session for a token-rotation transaction
- **WHEN** the calling request is cancelled while the refresh is mid-rollback
- **THEN** the rollback runs to completion against its own session
- **AND** no `BaseException` originating inside `session.rollback()` or
  `session.close()` escapes from `safe_rollback` / `safe_close`
- **AND** the background engine pool does not accumulate a stranded connection
  attributable to this path

#### Scenario: request log write site uses the canonical helper

- **GIVEN** `RequestLogsRepository.add_log` or
  `RequestLogsRepository.update_model_for_request` must roll back on commit
  failure
- **WHEN** it invokes the rollback helper
- **THEN** it imports `safe_rollback` from `app.db.session`
- **AND** it does not define its own rollback coroutine at module scope

#### Scenario: helper names are importable across modules

- **GIVEN** a module outside `app.db.session` needs to perform cancel-safe
  cleanup
- **WHEN** that module imports `safe_rollback` or `safe_close`
- **THEN** the import succeeds (no leading underscore on the symbol)
- **AND** the imported symbol is the same coroutine object exported by
  `app.db.session`

### Requirement: Cancel-safe cleanup helpers are exercised by tests

`tests/unit/test_db_session.py` MUST contain regression coverage that asserts
`safe_close` and `safe_rollback` outlive caller cancellation: a fake session
whose close/rollback awaits a release event MUST complete cleanup even when
the calling task is cancelled mid-cleanup, and the helper's `finally` path
MUST run after the inner shielded task finishes.

#### Scenario: regression test references the public name

- **GIVEN** the consolidation refactor renames the helpers to `safe_close`
  and `safe_rollback`
- **WHEN** the regression test is invoked
- **THEN** it references `session_module.safe_close` and
  `session_module.safe_rollback`
- **AND** the cancellation contract still passes under the renamed symbol

## REMOVED Requirements

(None — the existing "Detached background tasks own their database session
lifetime" requirement is preserved verbatim and continues to govern the
caller side of the cleanup contract.)