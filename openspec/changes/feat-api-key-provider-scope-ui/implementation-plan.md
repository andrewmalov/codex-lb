# feat-api-key-provider-scope-ui Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expose `provider_scope` as a single-choice `Provider` radio control in the **Create API key** dialog and as a read-only badge in the **Edit API key** dialog, so operators can authorize keys for `codex` or `claude` without sending `POST /api/api-keys/` by hand.

**Architecture:** Frontend-only. Inline `<input type="radio">` group with the same visual idiom as the existing "Apply to codex /model" checkbox row. The dialog's Zod form schema gains `providerScope: z.array(z.enum(["codex","claude"])).length(1, "Choose a provider")` so an empty selection blocks submit via `FormMessage`. The edit dialog never sends `providerScope` in PATCH. Backend is unchanged — it already accepts single-element arrays and the validator returns the chosen scope unchanged.

**Tech Stack:** React 19, react-hook-form + zod, vitest + Testing Library, Tailwind, shadcn/ui (existing components). Backend tests already cover the API surface — no new backend work. No new i18n keys (existing API-keys dialog strings are hardcoded English; matching that convention is YAGNI).

---

## File Structure

| Path | Role | Action |
|---|---|---|
| `openspec/changes/feat-api-key-provider-scope-ui/{proposal,tasks,implementation-plan}.md` | OpenSpec artifacts | **Already written** |
| `openspec/changes/feat-api-key-provider-scope-ui/specs/api-keys/spec.md` | Normative delta spec | **Already written** |
| `frontend/src/features/api-keys/components/api-key-create-dialog.tsx` | Add `Provider` radio + Zod field + draft state + payload | Modify |
| `frontend/src/features/api-keys/components/api-key-edit-dialog.tsx` | Add read-only `Provider:` badge | Modify |
| `frontend/src/features/api-keys/components/api-key-create-dialog.test.tsx` | Add 4 new tests; update 5 existing tests' fixtures | Modify |
| `frontend/src/features/api-keys/components/api-key-edit-dialog.test.tsx` | Add 1 new test | Modify |
| `frontend/src/test/mocks/factories.ts` | Set `providerScope: ["codex"]` default in `createApiKey` factory | Modify |

No backend changes, no DB migration, no dependency bumps.

---

## Task 1: Create worktree + branch

**Files:** none (git only)

- [ ] **Step 1: Confirm clean main**

```bash
git -C /Users/amalov/codex-lb fetch origin
git -C /Users/amalov/codex-lb status --short
```

Expected: `git status` is empty or only lists `openspec/changes/diagnose-claude-oauth-add-blocker/` (untracked, owned by another session — do not touch).

- [ ] **Step 2: Create worktree and branch**

```bash
git worktree add /Users/amalov/codex-lb-api-key-provider-scope-ui \
    -b feat/api-key-provider-scope-ui origin/main
cd /Users/amalov/codex-lb-api-key-provider-scope-ui
git status --short
```

Expected: clean working tree on `feat/api-key-provider-scope-ui`, `git rev-parse --abbrev-ref HEAD` reports the branch name.

- [ ] **Step 3: Sanity-check the local frontend build**

```bash
cd frontend && bun install --frozen-lockfile && bun run typecheck
```

Expected: `typecheck` exits 0. (Don't run `bun run test` yet — we'll do that after edits.)

---

## Task 2: Update `createApiKey` factory to include `providerScope` default

**Files:**
- Modify: `frontend/src/test/mocks/factories.ts:610-639`

- [ ] **Step 1: Write the failing test**

There is no existing test asserting the factory's `providerScope` default. Add a focused test in `api-key-edit-dialog.test.tsx`'s helper area OR a new file `frontend/src/test/mocks/factories.test.ts`. Simpler: rely on the visible behavior in Task 4 — we'll add a `test_create_dialog_payload_includes_provider_scope_codex` test that uses `createApiKey({providerScope: ["codex"]})` and that will fail until the factory supports the override.

Actually, the simplest path: directly edit the factory in this task and rely on later tests to catch any break.

- [ ] **Step 2: Add `providerScope` default to `createApiKey`**

In `frontend/src/test/mocks/factories.ts` around line 624 (inside `createApiKey`), add `providerScope: ["codex"]` to the literal:

```ts
export function createApiKey(overrides: Partial<ApiKey> = {}): ApiKey {
  return ApiKeySchema.parse({
    id: "key_1",
    name: "Default key",
    keyPrefix: "sk-test",
    allowedModels: ["gpt-5.1"],
    applyToCodexModel: false,
    transportPolicyOverride: null,
    expiresAt: null,
    isActive: true,
    accountAssignmentScopeEnabled: false,
    assignedAccountIds: [],
    providerScope: ["codex"],     // <-- ADD THIS LINE
    createdAt: offsetIso(-60),
    lastUsedAt: offsetIso(-5),
    usageSummary: { /* unchanged */ },
    limits: [ /* unchanged */ ],
    ...overrides,
  });
}
```

- [ ] **Step 3: Run existing edit-dialog tests to confirm no regression**

```bash
cd frontend && bun run test -- api-key-edit-dialog
```

Expected: all 12 existing tests still pass (no test depends on `providerScope` being absent). If any fail because of the new field, fix the test to pass `providerScope: ["codex"]` in its fixture via `createApiKey({ providerScope: ["claude"] })` where appropriate.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/test/mocks/factories.ts
git commit -m "test(factories): default createApiKey providerScope to [\"codex\"]"
```

---

## Task 3: Add failing test for `Provider` radio render in create dialog

**Files:**
- Modify: `frontend/src/features/api-keys/components/api-key-create-dialog.test.tsx`

- [ ] **Step 1: Add the failing test**

Insert after the existing `it("shows the codex /model checkbox unchecked by default", ...)` (around line 25):

```ts
  it("renders the Provider radio with no default selection", () => {
    renderWithProviders(
      <ApiKeyCreateDialog
        open
        busy={false}
        onOpenChange={vi.fn()}
        onSubmit={vi.fn()}
      />,
    );

    const codex = screen.getByRole("radio", { name: "Codex" });
    const claude = screen.getByRole("radio", { name: "Claude" });
    expect(codex).not.toBeChecked();
    expect(claude).not.toBeChecked();
    // Mandatory marker is present (label has the asterisk).
    expect(screen.getByText("Provider", { exact: false })).toBeInTheDocument();
  });
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
cd frontend && bun run test -- api-key-create-dialog -t "renders the Provider radio"
```

Expected: FAIL with message about `Unable to find a radio with the name "Codex"` — the control does not exist yet.

- [ ] **Step 3: Commit the failing test (TDD discipline)**

```bash
git add frontend/src/features/api-keys/components/api-key-create-dialog.test.tsx
git commit -m "test(api-key-create-dialog): assert Provider radio renders with no default"
```

---

## Task 4: Implement `Provider` radio + Zod schema + draft + payload in create dialog

**Files:**
- Modify: `frontend/src/features/api-keys/components/api-key-create-dialog.tsx`

- [ ] **Step 1: Extend the form schema**

In `api-key-create-dialog.tsx`, replace the existing `formSchema` block (lines 45-47) with:

```ts
const formSchema = z.object({
  name: z.string().min(1, "Name is required"),
  providerScope: z
    .array(z.enum(["codex", "claude"]))
    .length(1, "Choose a provider"),
});
```

- [ ] **Step 2: Add `providerScope` to draft type + initial state**

Update `ApiKeyCreateDraft` (around line 64-76) and `initialApiKeyCreateDraft` (78-90):

```ts
type ApiKeyCreateDraft = {
  selectedModels: string[];
  selectedAccountIds: string[];
  usageSections: string;
  limitRules: LimitRuleCreate[];
  expiresAt: Date | null;
  enforcedModel: string;
  enforcedReasoningEffort: string;
  enforcedServiceTier: string;
  trafficClass: TrafficClass;
  transportPolicyOverride: TransportPolicyOverride | null;
  applyToCodexModel: boolean;
  providerScope: ("codex" | "claude")[];
};

const initialApiKeyCreateDraft: ApiKeyCreateDraft = {
  selectedModels: [],
  selectedAccountIds: [],
  usageSections: "upstream_limits,account_pool_usage",
  limitRules: [],
  expiresAt: null,
  enforcedModel: "",
  enforcedReasoningEffort: "none",
  enforcedServiceTier: "none",
  trafficClass: "foreground",
  transportPolicyOverride: null,
  applyToCodexModel: false,
  providerScope: [],   // <-- ADD: no default; user must pick
};
```

- [ ] **Step 3: Include `providerScope` in the form defaults**

Update the `useForm` block (around line 100-103):

```ts
const form = useForm<FormValues>({
  resolver: zodResolver(formSchema),
  defaultValues: { name: "", providerScope: [] },
  mode: "onSubmit",
});
```

`mode: "onSubmit"` ensures the error appears when the user clicks Create, not as they interact.

- [ ] **Step 4: Always include `providerScope` in the submit payload**

Update `handleSubmit` (around line 107-125) — extend the `payload` literal:

```ts
const handleSubmit = async (values: FormValues) => {
  const validLimits = draft.limitRules.filter((rule) => rule.maxValue > 0);
  const payload: ApiKeyCreateRequest = {
    name: values.name,
    providerScope: draft.providerScope,
    allowedModels: draft.selectedModels.length > 0 ? draft.selectedModels : undefined,
    applyToCodexModel: draft.applyToCodexModel,
    ...(draft.selectedAccountIds.length > 0 ? { assignedAccountIds: draft.selectedAccountIds } : {}),
    usageSections: draft.usageSections,
    enforcedModel: draft.enforcedModel.trim() ? draft.enforcedModel.trim() : null,
    enforcedReasoningEffort:
      draft.enforcedReasoningEffort === "none"
        ? null
        : draft.enforcedReasoningEffort as "minimal" | "low" | "medium" | "high" | "xhigh",
    enforcedServiceTier: draft.enforcedServiceTier === "none" ? null : draft.enforcedServiceTier as ServiceTierType,
    trafficClass: draft.trafficClass,
    transportPolicyOverride: draft.transportPolicyOverride,
    expiresAt: draft.expiresAt?.toISOString(),
    limits: validLimits.length > 0 ? validLimits : undefined,
  };
  // ...rest unchanged
};
```

- [ ] **Step 5: Render the radio group between `ModelMultiSelect` and the "Apply to codex /model" row**

Find the block after `ModelMultiSelect` (around line 159) and the `Apply to codex /model` checkbox row (lines 162-171). Insert between them:

```tsx
<div className="space-y-1">
  <p className="text-sm font-medium">
    Provider <span className="text-destructive">*</span>
  </p>
  <Controller
    control={form.control}
    name="providerScope"
    render={({ field, fieldState }) => (
      <>
        <div className="flex gap-4">
          <label className="flex cursor-pointer items-center gap-2 rounded-md border p-2 text-sm">
            <input
              type="radio"
              name="create-api-key-provider"
              value="codex"
              checked={field.value[0] === "codex"}
              onChange={() => {
                updateDraft({ providerScope: ["codex"] });
                field.onChange(["codex"]);
              }}
            />
            <span>Codex</span>
          </label>
          <label className="flex cursor-pointer items-center gap-2 rounded-md border p-2 text-sm">
            <input
              type="radio"
              name="create-api-key-provider"
              value="claude"
              checked={field.value[0] === "claude"}
              onChange={() => {
                updateDraft({ providerScope: ["claude"] });
                field.onChange(["claude"]);
              }}
            />
            <span>Claude</span>
          </label>
        </div>
        {fieldState.error ? (
          <p className="text-xs text-destructive">{fieldState.error.message}</p>
        ) : null}
      </>
    )}
  />
</div>
```

Import `Controller` at the top of the file:

```ts
import { Controller } from "react-hook-form";
```

(`useForm` import line is already present; just add `Controller`.)

- [ ] **Step 6: Run the new test to verify it now passes**

```bash
cd frontend && bun run test -- api-key-create-dialog -t "renders the Provider radio"
```

Expected: PASS.

- [ ] **Step 7: Run all create-dialog tests — expect 5 to fail because they submit without picking a provider**

```bash
cd frontend && bun run test -- api-key-create-dialog
```

Expected: 5 tests fail (the ones that click Create). Each failure will mention either "Choose a provider" or that the test is being blocked by validation. That's expected; we'll fix the tests in Task 5.

- [ ] **Step 8: Commit**

```bash
git add frontend/src/features/api-keys/components/api-key-create-dialog.tsx
git commit -m "feat(api-key-create-dialog): add Provider radio with required validation"
```

---

## Task 5: Update existing create-dialog tests to pre-select a provider

**Files:**
- Modify: `frontend/src/features/api-keys/components/api-key-create-dialog.test.tsx`

The 5 submit-tests below currently click Create without selecting a provider; they need to click Codex first. The pattern: after typing the name, do `await user.click(screen.getByRole("radio", { name: "Codex" }))` before `Create`.

- [ ] **Step 1: Update "submits the codex /model checkbox value" (line 27)**

```ts
  it("submits the codex /model checkbox value", async () => {
    const user = userEvent.setup();
    const onSubmit = vi.fn().mockResolvedValue(undefined);

    renderWithProviders(
      <ApiKeyCreateDialog
        open
        busy={false}
        onOpenChange={vi.fn()}
        onSubmit={onSubmit}
      />,
    );

    await user.type(screen.getByLabelText("Name"), "Codex key");
    await user.click(screen.getByRole("radio", { name: "Codex" }));    // ADD
    await user.click(screen.getByRole("checkbox", { name: "Apply to codex /model" }));
    await user.click(screen.getByRole("button", { name: "Create" }));

    await waitFor(() => {
      expect(onSubmit).toHaveBeenCalledTimes(1);
    });

    expect(onSubmit.mock.calls[0][0].applyToCodexModel).toBe(true);
    expect(onSubmit.mock.calls[0][0].providerScope).toEqual(["codex"]);   // ADD
  });
```

- [ ] **Step 2: Update "submits opportunistic traffic class" (line 51)**

Same pattern: add the Codex click before the traffic-class combobox interaction. Add an `expect(onSubmit.mock.calls[0][0].providerScope).toEqual(["codex"])` assertion.

- [ ] **Step 3: Update "renders and submits a transport policy override" (line 76)**

Same pattern: add the Codex click. Add `expect(onSubmit.mock.calls[0][0].providerScope).toEqual(["codex"])`.

- [ ] **Step 4: Update "omits assigned accounts when left at all accounts" (line 142)**

Same pattern: add the Codex click. Replace the existing `expect("assignedAccountIds" in payload).toBe(false)` block with both assertions.

- [ ] **Step 5: Update "submits selected assigned accounts on create" (line 167)**

Same pattern: add the Codex click. The test already asserts `assignedAccountIds`; add the `providerScope: ["codex"]` assertion.

- [ ] **Step 6: Run all create-dialog tests**

```bash
cd frontend && bun run test -- api-key-create-dialog
```

Expected: all 9 tests pass (5 updated + 4 untouched: `shows the codex /model checkbox unchecked by default`, `resets the codex /model checkbox when the dialog is dismissed`, `clears selected assigned accounts when the dialog is dismissed`, `renders the Provider radio with no default selection`).

- [ ] **Step 7: Commit**

```bash
git add frontend/src/features/api-keys/components/api-key-create-dialog.test.tsx
git commit -m "test(api-key-create-dialog): pre-select provider in submit tests + assert providerScope in payload"
```

---

## Task 6: Add tests for blocked submit, codex payload, claude payload

**Files:**
- Modify: `frontend/src/features/api-keys/components/api-key-create-dialog.test.tsx`

- [ ] **Step 1: Add "blocks submit when no provider is selected"**

Insert after the `renders the Provider radio` test (i.e., after Task 3's addition):

```ts
  it("blocks submit when no provider is selected", async () => {
    const user = userEvent.setup();
    const onSubmit = vi.fn().mockResolvedValue(undefined);

    renderWithProviders(
      <ApiKeyCreateDialog
        open
        busy={false}
        onOpenChange={vi.fn()}
        onSubmit={onSubmit}
      />,
    );

    await user.type(screen.getByLabelText("Name"), "No provider");
    await user.click(screen.getByRole("button", { name: "Create" }));

    await waitFor(() => {
      expect(screen.getByText("Choose a provider")).toBeInTheDocument();
    });
    expect(onSubmit).not.toHaveBeenCalled();
  });
```

- [ ] **Step 2: Add "payload includes providerScope=['codex'] when Codex selected"**

```ts
  it("payload includes providerScope=['codex'] when Codex is selected", async () => {
    const user = userEvent.setup();
    const onSubmit = vi.fn().mockResolvedValue(undefined);

    renderWithProviders(
      <ApiKeyCreateDialog
        open
        busy={false}
        onOpenChange={vi.fn()}
        onSubmit={onSubmit}
      />,
    );

    await user.type(screen.getByLabelText("Name"), "Codex only");
    await user.click(screen.getByRole("radio", { name: "Codex" }));
    await user.click(screen.getByRole("button", { name: "Create" }));

    await waitFor(() => {
      expect(onSubmit).toHaveBeenCalledTimes(1);
    });
    expect(onSubmit.mock.calls[0][0].providerScope).toEqual(["codex"]);
  });
```

- [ ] **Step 3: Add "payload includes providerScope=['claude'] when Claude selected"**

```ts
  it("payload includes providerScope=['claude'] when Claude is selected", async () => {
    const user = userEvent.setup();
    const onSubmit = vi.fn().mockResolvedValue(undefined);

    renderWithProviders(
      <ApiKeyCreateDialog
        open
        busy={false}
        onOpenChange={vi.fn()}
        onSubmit={onSubmit}
      />,
    );

    await user.type(screen.getByLabelText("Name"), "Claude only");
    await user.click(screen.getByRole("radio", { name: "Claude" }));
    await user.click(screen.getByRole("button", { name: "Create" }));

    await waitFor(() => {
      expect(onSubmit).toHaveBeenCalledTimes(1);
    });
    expect(onSubmit.mock.calls[0][0].providerScope).toEqual(["claude"]);
  });
```

- [ ] **Step 4: Run all create-dialog tests**

```bash
cd frontend && bun run test -- api-key-create-dialog
```

Expected: 12 tests pass (4 from Task 5 + 3 from this task + 5 untouched).

- [ ] **Step 5: Commit**

```bash
git add frontend/src/features/api-keys/components/api-key-create-dialog.test.tsx
git commit -m "test(api-key-create-dialog): cover blocked submit and codex/claude payload"
```

---

## Task 7: Add failing test for read-only `Provider` badge in edit dialog

**Files:**
- Modify: `frontend/src/features/api-keys/components/api-key-edit-dialog.test.tsx`

- [ ] **Step 1: Add the failing test**

Insert at the bottom of the `describe("ApiKeyEditDialog", ...)` block (before the closing `});` around line 426):

```ts
  it("displays the provider scope as a read-only badge", () => {
    renderWithProviders(
      <ApiKeyEditDialog
        open
        busy={false}
        apiKey={createApiKey({ providerScope: ["claude"] })}
        onOpenChange={vi.fn()}
        onSubmit={vi.fn()}
      />,
    );

    // The badge text is visible.
    expect(screen.getByText(/Provider:/)).toBeInTheDocument();
    expect(screen.getByText(/Claude/)).toBeInTheDocument();

    // No radio control exists in the edit dialog.
    expect(screen.queryByRole("radio", { name: "Codex" })).not.toBeInTheDocument();
    expect(screen.queryByRole("radio", { name: "Claude" })).not.toBeInTheDocument();
  });

  it("does not include providerScope in PATCH payload when saving", async () => {
    const user = userEvent.setup();
    const onSubmit = vi.fn().mockResolvedValue(undefined);

    renderWithProviders(
      <ApiKeyEditDialog
        open
        busy={false}
        apiKey={createApiKey({ providerScope: ["claude"] })}
        onOpenChange={vi.fn()}
        onSubmit={onSubmit}
      />,
    );

    const nameInput = screen.getByLabelText("Name");
    await user.clear(nameInput);
    await user.type(nameInput, "Renamed claude key");
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => {
      expect(onSubmit).toHaveBeenCalledTimes(1);
    });

    const payload = onSubmit.mock.calls[0][0];
    expect("providerScope" in payload).toBe(false);
  });
```

- [ ] **Step 2: Run the new tests to verify they fail**

```bash
cd frontend && bun run test -- api-key-edit-dialog -t "provider scope as a read-only badge"
bun run test -- api-key-edit-dialog -t "does not include providerScope in PATCH payload"
```

Expected: both fail. First fails because the badge text doesn't exist. Second actually passes (since current payload doesn't include `providerScope`) — that's OK, the test is forward-looking; it pins the invariant.

- [ ] **Step 3: Commit the failing test**

```bash
git add frontend/src/features/api-keys/components/api-key-edit-dialog.test.tsx
git commit -m "test(api-key-edit-dialog): assert provider scope badge is read-only"
```

---

## Task 8: Implement the read-only badge in edit dialog

**Files:**
- Modify: `frontend/src/features/api-keys/components/api-key-edit-dialog.tsx`

- [ ] **Step 1: Add a `providerScopeLabel` helper**

Above `ApiKeyEditForm` (around line 128), add:

```ts
function providerScopeLabel(apiKey: ApiKey): "Codex" | "Claude" {
  return apiKey.providerScope?.[0] === "claude" ? "Claude" : "Codex";
}
```

The `?.` handles legacy keys where the field is undefined (defensive — backend default guarantees `["codex"]`, but the type allows `undefined`).

- [ ] **Step 2: Render the badge between Name and Allowed models**

In `api-key-edit-dialog.tsx` (around line 192-194, just after the `Name` FormField and before the `Allowed models` div), insert:

```tsx
<div className="flex items-center gap-2 rounded-md border bg-muted/40 p-2 text-sm">
  <span className="text-muted-foreground">Provider:</span>
  <strong>{providerScopeLabel(apiKey)}</strong>
  <span className="ml-auto text-xs text-muted-foreground">(immutable after creation)</span>
</div>
```

- [ ] **Step 3: Run all edit-dialog tests**

```bash
cd frontend && bun run test -- api-key-edit-dialog
```

Expected: 14 tests pass (12 existing + 2 new). If a test fails because the badge text breaks another assertion (unlikely — it's just a div with text), adjust the assertion.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/features/api-keys/components/api-key-edit-dialog.tsx
git commit -m "feat(api-key-edit-dialog): show provider scope as read-only badge"
```

---

## Task 9: Full local verification

- [ ] **Step 1: Frontend unit tests**

```bash
cd frontend && bun run test
```

Expected: all tests pass.

- [ ] **Step 2: Frontend lint**

```bash
cd frontend && bun run lint
```

Expected: no errors. Fix any `react/no-unescaped-entities` or `react-hooks/exhaustive-deps` warnings introduced by the new radio group.

- [ ] **Step 3: Frontend typecheck**

```bash
cd frontend && bun run typecheck
```

Expected: 0 errors.

- [ ] **Step 4: Backend tests — sanity (unchanged backend should still pass)**

```bash
cd /Users/amalov/codex-lb-api-key-provider-scope-ui && uv run pytest tests/unit/test_api_key_provider_scope_validator.py tests/unit/test_api_key_model_provider_scope.py tests/integration/test_api_keys_provider_scope.py tests/integration/test_claude_api.py -v
```

Expected: all pass. We're not changing backend, but a quick smoke confirms we didn't break anything in shared types.

- [ ] **Step 5: Repo-wide lint + typecheck (per CLAUDE.md — CI catches what local-scope checks miss)**

```bash
make lint
make typecheck
```

Expected: clean. If `make` target is missing, fall back to `ruff check .` and `uv run ty check .` from repo root.

- [ ] **Step 6: Pre-commit hooks**

```bash
pre-commit run --all-files
```

Expected: all hooks pass. First-time setup may need `pre-commit install` (one-time per clone).

- [ ] **Step 7: OpenSpec strict validation**

```bash
openspec validate feat-api-key-provider-scope-ui --strict --no-interactive
```

Expected: `Change 'feat-api-key-provider-scope-ui' is valid`.

---

## Task 10: Commit, push, open PR

- [ ] **Step 1: Confirm clean working tree in worktree**

```bash
cd /Users/amalov/codex-lb-api-key-provider-scope-ui
git status --short
git log --oneline origin/main..HEAD
```

Expected: clean. Commits on the branch:
1. `test(factories): default createApiKey providerScope to ["codex"]`
2. `test(api-key-create-dialog): assert Provider radio renders with no default`
3. `feat(api-key-create-dialog): add Provider radio with required validation`
4. `test(api-key-create-dialog): pre-select provider in submit tests + assert providerScope in payload`
5. `test(api-key-create-dialog): cover blocked submit and codex/claude payload`
6. `test(api-key-edit-dialog): assert provider scope badge is read-only`
7. `feat(api-key-edit-dialog): show provider scope as read-only badge`

- [ ] **Step 2: Push branch**

```bash
git push -u origin feat/api-key-provider-scope-ui
```

- [ ] **Step 3: Open PR**

```bash
gh pr create --base main \
  --title "feat(api-keys): expose provider_scope selector in create dialog" \
  --body "$(cat <<'EOF'
## Why

The `api_keys.provider_scope` column, the backend validator, and the proxy
guard (`provider_scope_mismatch` on `/claude/v1/*` for codex-only keys) were
all delivered by `add-claude-oauth-pool`. The dashboard UI never exposed a
control to pick a provider, so every key created through the dialog ends up
with the backend default `["codex"]` — operators cannot authorize Claude
traffic without sending `POST /api/api-keys/` by hand.

This PR closes the UI gap without changing the backend.

## What

- New `Provider` radio group (Codex / Claude) in the Create API key dialog.
  Required selection — submit is blocked with a field-level error if neither
  option is chosen. Single-choice only.
- Read-only `Provider:` badge in the Edit API key dialog. Scope is immutable
  after creation (regenerate required to change).
- 7 new tests across both dialogs + 5 existing tests updated to pre-select
  a provider. No backend tests added (backend unchanged).
- i18n: skipped deliberately — the rest of the API-key dialog is hardcoded
  English, and adding `t()` only for the new field would be inconsistent.

## Verification

- `bun run test` — green
- `bun run lint` + `bun run typecheck` — green
- `make lint` + `make typecheck` (full repo, per CLAUDE.md) — green
- `pre-commit run --all-files` — green
- `openspec validate feat-api-key-provider-scope-ui --strict --no-interactive` — green

## Notes

- Backend stays flexible: `provider_scope: ["codex","claude"]` still works
  via curl. The single-choice rule is a UI constraint, not an API constraint.
- Existing keys remain `provider_scope='codex'` — no migration.
- Spec delta lives at `openspec/changes/feat-api-key-provider-scope-ui/`.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 4: Confirm CI green + `mergeable=CLEAN`**

```bash
gh pr checks
gh pr view --json mergeable,statusCheckRollup
```

Expected: all checks pass, `mergeable=CLEAN`. Hand off to reviewer; do not self-merge (per `.github/CONTRIBUTING.md` collaborator rules).

---

## Self-Review Notes

- **Spec coverage:**
  - "renders radio with no default" → Task 3 + Task 4 step 5
  - "blocks submit when no provider is selected" → Task 6 step 1
  - "sends providerScope=['codex']" → Task 6 step 2
  - "sends providerScope=['claude']" → Task 6 step 3
  - "edit dialog displays read-only badge" → Task 7 + Task 8
  - "edit dialog does not send providerScope in PATCH" → Task 7 step 1 (second test) + Task 8 step 1 (verifies handleSubmit does not add the field)
- **Placeholder scan:** no TBD / TODO / "implement later" anywhere. All code blocks are complete.
- **Type consistency:** `providerScope` is `("codex" | "claude")[]` everywhere — matches `ApiKeyCreateRequest.providerScope` in `schemas.ts:103` and `ApiKey.providerScope` in `schemas.ts:72`. `providerScopeLabel` returns `"Codex" | "Claude"` consistently.
- **Ambiguity check:** the badge's `providerScope?.[0]` fallback handles legacy keys with `providerScope: undefined` (the type allows it via `.optional()`). Backend default guarantees `["codex"]`, but the defensive read costs nothing.
- **Scope check:** single concern (UI gap for provider_scope), single PR, no backend drift.