# Tasks

## 1. Setup

- [ ] 1.1 Create a worktree and branch off `origin/main`:
      `git worktree add /Users/amalov/codex-lb-api-key-provider-scope-ui -b feat/api-key-provider-scope-ui origin/main`
- [ ] 1.2 Confirm `pnpm` (or repo-equivalent) install + lint scripts work locally.

## 2. i18n

- [x] 2.1 Add the following keys to `frontend/src/i18n/locales/en.json` under
      `apiKeys.providerScope`: `label`, `required`, `codex`, `claude`,
      `readOnlyBadge`, `immutableHint`. _DEFERRED — see Implementation notes:_
      _the rest of the API-key dialog is hardcoded English, and adding_
      _`t()` only for the new field would be inconsistent._
- [x] 2.2 Mirror the same keys with zh-CN translations in
      `frontend/src/i18n/locales/zh-CN.json`. _DEFERRED for the same reason as 2.1._

## 3. Create dialog

- [x] 3.1 Extend `formSchema` in
      `frontend/src/features/api-keys/components/api-key-create-dialog.tsx`
      with `providerScope: z.array(z.enum(["codex","claude"])).length(1, ...)`.
      _Done in 3a246bcb._
- [x] 3.2 _REMOVED during implementation (refactor d7ac62c6): providerScope_
      _is owned by react-hook-form via Controller, not the useReducer draft._
      _This keeps a single source of truth and matches how `name` works._
- [x] 3.3 Render a `RadioGroup` between `ModelMultiSelect` and the
      `Apply to codex /model` checkbox, with two items (`Codex`, `Claude`)
      driven by `field.value[0]` and a mandatory asterisk on the label.
      _Done in 3a246bcb._
- [x] 3.4 Update `handleSubmit` so it always includes
      `providerScope: values.providerScope` in the payload
      _(reads from RHF, not draft)_.

## 4. Edit dialog

- [ ] 4.1 Render a read-only badge between `Name` and `Allowed models` in
      `frontend/src/features/api-keys/components/api-key-edit-dialog.tsx`
      showing `Provider: <Codex|Claude>` with a hint that scope is immutable.
- [ ] 4.2 Confirm `handleSubmit` does NOT include `providerScope` in the
      PATCH payload (current behavior — leave as-is).

## 5. Tests

- [ ] 5.1 Add to `api-key-create-dialog.test.tsx`:
      - `test_create_dialog_renders_provider_radio_with_no_default_selection`
      - `test_create_dialog_blocks_submit_when_no_provider_selected`
      - `test_create_dialog_payload_includes_provider_scope_codex`
      - `test_create_dialog_payload_includes_provider_scope_claude`
- [ ] 5.2 Add to `api-key-edit-dialog.test.tsx`:
      - `test_edit_dialog_displays_provider_scope_as_readonly_badge`
- [ ] 5.3 Update any existing tests in `api-key-create-dialog.test.tsx` that
      assume a successful submit with no provider selection — they should
      now pre-select a provider in their fixture.

## 6. Verification

- [ ] 6.1 `pnpm test` (or repo-equivalent) — green.
- [ ] 6.2 `pnpm run lint` — green.
- [ ] 6.3 `pnpm run typecheck` — green.
- [ ] 6.4 `make lint` (full repo, per CLAUDE.md) — green.
- [ ] 6.5 `make typecheck` (full repo, per CLAUDE.md) — green.
- [ ] 6.6 `pre-commit run --all-files` — green.
- [ ] 6.7 `openspec validate feat-api-key-provider-scope-ui --strict --no-interactive` — green.

## 7. Commit + PR

- [ ] 7.1 Commit with conventional message
      `feat(api-keys): expose provider_scope selector in create dialog`.
- [ ] 7.2 `git push -u origin feat/api-key-provider-scope-ui`.
- [ ] 7.3 `gh pr create` with a body that explains the UI gap, links the
      diagnosis, and notes the backend is unchanged. Include `Fixes #N` /
      `Closes #N` only if a tracking issue exists.
- [ ] 7.4 Confirm CI green + `mergeable=CLEAN` on the PR before handing off.