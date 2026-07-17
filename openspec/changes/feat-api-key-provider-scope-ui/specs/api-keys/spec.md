# api-keys — UI provider_scope delta

## ADDED Requirements

### Requirement: API Key create dialog exposes provider_scope as a single-choice control

The Create API key dialog SHALL expose a `Provider` control that lets the
operator pick exactly one of `codex` or `claude` before submitting. The
control SHALL be a radio group with no pre-selected default. The dialog
SHALL block submit and surface a field-level error when neither option is
selected. The dialog SHALL always include `providerScope` in the
`POST /api/api-keys/` payload as a single-element JSON array. The dialog
SHALL NOT offer a "both" option; the UI SHALL refuse to dispatch a payload
containing more than one element.

#### Scenario: Create dialog renders radio with no default

- **WHEN** the operator opens the Create API key dialog
- **THEN** both `Codex` and `Claude` radio buttons are visible
- **AND** neither radio is pre-selected

#### Scenario: Create dialog blocks submit when no provider is selected

- **WHEN** the operator fills in `name` and clicks Create without picking a provider
- **THEN** the dialog displays a field-level error `Choose a provider`
- **AND** `POST /api/api-keys/` is NOT called

#### Scenario: Create dialog sends providerScope=['codex'] when Codex is selected

- **WHEN** the operator selects `Codex` and clicks Create
- **THEN** the dialog calls `POST /api/api-keys/` with `providerScope: ["codex"]`
- **AND** the created key has `providerScope: ["codex"]`

#### Scenario: Create dialog sends providerScope=['claude'] when Claude is selected

- **WHEN** the operator selects `Claude` and clicks Create
- **THEN** the dialog calls `POST /api/api-keys/` with `providerScope: ["claude"]`
- **AND** the created key has `providerScope: ["claude"]`

#### Scenario: Create dialog refuses to dispatch multi-element providerScope

- **WHEN** the form's Zod schema rejects `providerScope` with length > 1
- **THEN** submit is blocked
- **AND** the dialog never sends a payload with `providerScope: ["codex","claude"]` or any other multi-element value

### Requirement: API Key edit dialog renders provider_scope as read-only

The Edit API key dialog SHALL render the key's `providerScope` as a
read-only badge next to the key name. The dialog SHALL NOT render any
control that allows the operator to mutate `providerScope`. The
`PATCH /api/api-keys/{id}` payload constructed by the dialog SHALL NOT
include the `providerScope` field.

#### Scenario: Edit dialog displays provider badge for Codex key

- **WHEN** the operator opens Edit on a key with `providerScope: ["codex"]`
- **THEN** the dialog displays a read-only `Provider: Codex` badge
- **AND** no radio button or selectable control for `provider` is visible

#### Scenario: Edit dialog displays provider badge for Claude key

- **WHEN** the operator opens Edit on a key with `providerScope: ["claude"]`
- **THEN** the dialog displays a read-only `Provider: Claude` badge
- **AND** no radio button or selectable control for `provider` is visible

#### Scenario: Edit dialog does not send providerScope in PATCH payload

- **WHEN** the operator edits a key and clicks Save
- **THEN** the `PATCH /api/api-keys/{id}` request body does NOT contain
      `providerScope`
- **AND** the server keeps the key's existing `providerScope`

## MODIFIED Requirements

_None._

## REMOVED Requirements

_None._

## Notes

- Backend `ApiKeyCreateRequest.provider_scope` continues to accept arbitrary
  arrays (including `["codex","claude"]`) so that API/curl callers retain
  full flexibility. The single-choice policy above is a UI constraint, not
  an API constraint.
- This delta does NOT modify the backend schema, validator, DB column,
  proxy guard, or any existing spec requirement in `api-keys`. The
  existing delta from `add-claude-oauth-pool/specs/api-keys/spec.md`
  (`API Key creation accepts provider_scope`, `API Key update accepts
  provider_scope`, etc.) remains the source of truth for the API surface;
  this change adds UI behavior on top.
- Existing keys created before this change retain `provider_scope='codex'`
  and require no migration.