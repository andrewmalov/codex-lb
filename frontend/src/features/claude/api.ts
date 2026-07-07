import { patch, post, get } from "@/lib/api-client";

import {
  AddClaudeAccountRequestSchema,
  ClaudeAccountsResponseSchema,
  ClaudeAccountSchema,
  ClaudeOauthCallbackRequestSchema,
  ClaudeOauthCallbackResponseSchema,
  ClaudeOauthStartResponseSchema,
  ClaudeOauthStatusResponseSchema,
  DisableClaudeAccountRequestSchema,
  type AddClaudeAccountRequest,
  type ClaudeAccount,
  type ClaudeOauthCallbackRequest,
  type ClaudeOauthCallbackResponse,
  type ClaudeOauthStartResponse,
  type ClaudeOauthStatusResponse,
} from "@/features/claude/schemas";

const CLAUDE_BASE_PATH = "/api/claude/accounts";
const CLAUDE_OAUTH_BASE_PATH = "/api/claude/oauth";

export function listClaudeAccounts() {
  return get(CLAUDE_BASE_PATH, ClaudeAccountsResponseSchema);
}

export function addClaudeAccount(payload: AddClaudeAccountRequest) {
  const validated = AddClaudeAccountRequestSchema.parse(payload);
  return post(CLAUDE_BASE_PATH, ClaudeAccountSchema, { body: validated });
}

export function disableClaudeAccount(accountId: string, reason?: string) {
  const validated = reason === undefined
    ? undefined
    : DisableClaudeAccountRequestSchema.parse({ reason });
  return patch(
    `${CLAUDE_BASE_PATH}/${encodeURIComponent(accountId)}/disable`,
    null,
    validated ? { body: validated } : undefined,
  );
}

export function enableClaudeAccount(accountId: string) {
  return patch(
    `${CLAUDE_BASE_PATH}/${encodeURIComponent(accountId)}/enable`,
    null,
  );
}

export function startClaudeOauth(): Promise<ClaudeOauthStartResponse> {
  return post(`${CLAUDE_OAUTH_BASE_PATH}/start`, ClaudeOauthStartResponseSchema, {
    body: {},
  });
}

export function getClaudeOauthStatus(flowId: string): Promise<ClaudeOauthStatusResponse> {
  return get(
    `${CLAUDE_OAUTH_BASE_PATH}/status?flowId=${encodeURIComponent(flowId)}`,
    ClaudeOauthStatusResponseSchema,
  );
}

export function submitClaudeOauthCallback(
  payload: ClaudeOauthCallbackRequest,
): Promise<ClaudeOauthCallbackResponse> {
  const validated = ClaudeOauthCallbackRequestSchema.parse(payload);
  return post(`${CLAUDE_OAUTH_BASE_PATH}/callback`, ClaudeOauthCallbackResponseSchema, {
    body: validated,
  });
}

export type { ClaudeAccount };