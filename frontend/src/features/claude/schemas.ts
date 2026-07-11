import { z } from "zod";

export const ClaudeAccountSchema = z.object({
  id: z.string(),
  claudeAccountUuid: z.string(),
  userEmail: z.string().nullable().optional(),
  userOrganizationUuid: z.string().nullable().optional(),
  status: z.string().nullable().optional(),
  isActive: z.boolean(),
  claudeAccessTokenExpiresAt: z.string().nullable().optional(),
  lastUsedAt: z.string().nullable().optional(),
  rateLimitRequestsRemaining: z.number().nullable().optional(),
  rateLimitRequestsResetAt: z.string().nullable().optional(),
  rateLimitInputTokensRemaining: z.number().nullable().optional(),
  rateLimitInputTokensResetAt: z.string().nullable().optional(),
  rateLimitOutputTokensRemaining: z.number().nullable().optional(),
  rateLimitOutputTokensResetAt: z.string().nullable().optional(),
  rateLimitStatus: z.string().nullable().optional(),
  deactivationReason: z.string().nullable().optional(),
  createdAt: z.string(),
});

export type ClaudeAccount = z.infer<typeof ClaudeAccountSchema>;

export const ClaudeAccountsResponseSchema = z.object({
  accounts: z.array(ClaudeAccountSchema),
});

export const AddClaudeAccountRequestSchema = z.object({
  claudeAccountUuid: z.string().trim().min(1),
  accessToken: z.string().trim().min(1),
  refreshToken: z.string().trim().min(1),
  expiresInSeconds: z.number().int().positive().max(86400 * 30),
  scopes: z.array(z.string()).optional(),
  userEmail: z.union([z.literal(""), z.string().email()]).optional(),
  userOrganizationUuid: z.string().optional(),
});

export type AddClaudeAccountRequest = z.infer<typeof AddClaudeAccountRequestSchema>;

export const DisableClaudeAccountRequestSchema = z.object({
  reason: z.string().max(512).optional(),
});

export type DisableClaudeAccountRequest = z.infer<typeof DisableClaudeAccountRequestSchema>;

export const ClaudeOauthStartResponseSchema = z.object({
  flowId: z.string(),
  authorizationUrl: z.string().url(),
  stateToken: z.string(),
  expiresInSeconds: z.number().int().positive(),
  callbackInstructions: z.string(),
  redirectUri: z.string().url(),
});
export type ClaudeOauthStartResponse = z.infer<typeof ClaudeOauthStartResponseSchema>;

export const ClaudeOauthCallbackRequestSchema = z.object({
  flowId: z.string().min(1),
  code: z.string().min(1).max(4096),
  state: z.string().min(1).max(4096),
});
export type ClaudeOauthCallbackRequest = z.infer<typeof ClaudeOauthCallbackRequestSchema>;

export const ClaudeOauthCallbackResponseSchema = z.object({
  status: z.literal("success"),
  account: ClaudeAccountSchema,
});
export type ClaudeOauthCallbackResponse = z.infer<typeof ClaudeOauthCallbackResponseSchema>;

export const ClaudeOauthStatusResponseSchema = z.object({
  flowId: z.string(),
  status: z.enum(["pending", "success", "error"]),
  errorMessage: z.string().nullable().optional(),
  errorCode: z.string().nullable().optional(),
  accountId: z.string().nullable().optional(),
  startedAt: z.string(),
  finishedAt: z.string().nullable().optional(),
});
export type ClaudeOauthStatusResponse = z.infer<typeof ClaudeOauthStatusResponseSchema>;