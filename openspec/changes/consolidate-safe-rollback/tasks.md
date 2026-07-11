# Tasks

## 1. Rename helpers in `app/db/session.py`

- [x] 1.1 Rename `_safe_rollback` → `safe_rollback` and `_safe_close` → `safe_close` in `app/db/session.py`
- [x] 1.2 Update the two internal call sites inside `get_background_session` and `get_session` to use the new public names
- [x] 1.3 Keep the `_shielded(...)` helper unchanged and continue to use it as the implementation detail inside `safe_rollback` / `safe_close`
- [x] 1.4 Confirm no other internal references in `app/db/session.py` use the old private names

## 2. Update `app/modules/request_logs/repository.py`

- [x] 2.1 Remove the local `_safe_rollback` definition at the bottom of the file
- [x] 2.2 Import `safe_rollback` from `app.db.session`
- [x] 2.3 Update the two call sites (`add_log`, `update_model_for_request`) to use the imported `safe_rollback`
- [x] 2.4 Drop the now-unused `import anyio` from the file if no other symbol from it is referenced

## 3. Update `app/modules/claude/auth_manager.py`

- [x] 3.1 Remove the local unshielded `_safe_rollback` definition
- [x] 3.2 Import `safe_rollback` from `app.db.session`
- [x] 3.3 Update the one call site (`_claude_refresh_session`) to use the imported `safe_rollback`
- [x] 3.4 Confirm the import is added in alphabetical order alongside the other `app.*` imports

## 4. Confirm `app/dependencies.py` is not a consumer of the old private names

- [x] 4.1 `app/dependencies.py` no longer imports `_safe_close` / `_safe_rollback` directly. A subsequent refactor moved both context helpers (`_accounts_repo_context`, `_proxy_repo_context`) onto `get_background_session()`, which encapsulates `safe_close` / `safe_rollback` internally. Nothing to change in this file.
- [x] 4.2 Verified via `grep -rn "_safe_close\|_safe_rollback" app/` against `origin/main` — no matches in `app/dependencies.py`. The only remaining `_safe_close*` symbols in the tree are in `app/core/clients/anthropic/chat.py`, where they close aiohttp responses (not SQLAlchemy sessions). Out of scope for this change.

## 5. Update tests

- [x] 5.1 In `tests/unit/test_db_session.py`, update the two regression tests (`test_safe_close_outlives_caller_cancellation`, `test_safe_rollback_outlives_caller_cancellation`) to reference `session_module.safe_close` / `session_module.safe_rollback`
- [x] 5.2 Run the tests in isolation to confirm the cancellation contract still holds under the public names

## 6. Verification

- [x] 6.1 `uvx ruff check app/db/session.py app/modules/request_logs/repository.py app/modules/claude/auth_manager.py tests/unit/test_db_session.py` — clean
- [x] 6.2 `uvx ruff format --check app/db/session.py app/modules/request_logs/repository.py app/modules/claude/auth_manager.py tests/unit/test_db_session.py` — clean
- [x] 6.3 `uv run ty check app/db/session.py app/modules/request_logs/repository.py app/modules/claude/auth_manager.py` — clean
- [x] 6.4 `uv run pytest tests/unit/test_db_session.py -k "safe_close or safe_rollback"` — green
- [x] 6.5 `uv run pytest tests/unit/ --ignore=tests/unit/test_upstream_sync_skill_layout.py` — green
- [x] 6.6 `openspec validate consolidate-safe-rollback --strict --no-interactive` — clean
- [x] 6.7 `grep -rn "_safe_close\|_safe_rollback" app/` returns no matches under `app/db/session.py` and `app/modules/*`

## 7. Branch & PR

- [ ] 7.1 Branch from `origin/main`: `git checkout -b refactor/consolidate-safe-rollback origin/main`
- [ ] 7.2 Single conventional commit: `refactor(db): consolidate safe_rollback / safe_close helpers` (no `Co-Authored-By` trailer — repo rule)
- [ ] 7.3 Push branch and open PR referencing the new OpenSpec change folder