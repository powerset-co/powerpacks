import type {
  EnvStatusResponse,
  LocalProfileResponse,
  LocalRunResultsResponse,
  LocalRunSummary,
  MessageReviewFilter,
  MessageReviewResponse,
  SetupJob,
  SetupStatusResponse,
} from "./types";

async function getJson<T>(path: string): Promise<T> {
  const response = await fetch(path);
  if (!response.ok) {
    const text = await response.text().catch(() => "");
    throw new Error(`${path} failed: ${response.status} ${text}`);
  }
  return response.json() as Promise<T>;
}

export function fetchRuns(): Promise<LocalRunSummary[]> {
  return getJson<LocalRunSummary[]>("/local-api/runs");
}

export function fetchEnvStatus(): Promise<EnvStatusResponse> {
  return getJson<EnvStatusResponse>("/local-api/env/status");
}

export function fetchLocalProfile(): Promise<LocalProfileResponse> {
  return getJson<LocalProfileResponse>("/local-api/profile");
}

export function fetchRunResults(
  taskId: string,
  options: { offset?: number; limit?: number } = {}
): Promise<LocalRunResultsResponse> {
  const params = new URLSearchParams();
  if (options.offset != null) params.set("offset", String(options.offset));
  if (options.limit != null) params.set("limit", String(options.limit));
  const query = params.toString() ? `?${params.toString()}` : "";
  return getJson<LocalRunResultsResponse>(`/local-api/runs/${encodeURIComponent(taskId)}/results${query}`);
}

export function fetchSetupStatus(options: { tab?: string } = {}): Promise<SetupStatusResponse> {
  const params = new URLSearchParams();
  if (options.tab) params.set("tab", options.tab);
  const query = params.toString() ? `?${params.toString()}` : "";
  return getJson<SetupStatusResponse>(`/local-api/setup/status${query}`);
}

export function fetchSetupJob(jobId: string): Promise<SetupJob> {
  return getJson<SetupJob>(`/local-api/setup/jobs/${encodeURIComponent(jobId)}`);
}

async function postJson<T>(path: string, body: Record<string, unknown>): Promise<T> {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    const text = await response.text().catch(() => "");
    throw new Error(`${path} failed: ${response.status} ${text}`);
  }
  return response.json() as Promise<T>;
}

export function runSetupAction(body: Record<string, unknown>): Promise<{ job: SetupJob }> {
  return postJson<{ job: SetupJob }>("/local-api/setup/run", body);
}

export function fetchOnboardingV2LinkedInStatus(): Promise<Record<string, unknown>> {
  return getJson<Record<string, unknown>>("/local-api/onboarding-v2/linkedin/status");
}

export function dryRunOnboardingV2LinkedIn(body: Record<string, unknown>): Promise<Record<string, unknown>> {
  return postJson<Record<string, unknown>>("/local-api/onboarding-v2/linkedin/dry-run", body);
}

export function runOnboardingV2LinkedIn(body: Record<string, unknown>): Promise<{ job: SetupJob; status: Record<string, unknown> }> {
  return postJson<{ job: SetupJob; status: Record<string, unknown> }>("/local-api/onboarding-v2/linkedin/run", body);
}

export function fetchOnboardingV2GmailStatus(): Promise<Record<string, unknown>> {
  return getJson<Record<string, unknown>>("/local-api/onboarding-v2/gmail/status");
}

export function dryRunOnboardingV2Gmail(body: Record<string, unknown> = {}): Promise<Record<string, unknown>> {
  return postJson<Record<string, unknown>>("/local-api/onboarding-v2/gmail/dry-run", body);
}

export function runOnboardingV2Gmail(body: Record<string, unknown> = {}): Promise<{ job: SetupJob; status: Record<string, unknown> }> {
  return postJson<{ job: SetupJob; status: Record<string, unknown> }>("/local-api/onboarding-v2/gmail/run", body);
}

export function checkGmailTokens(emails: string[]): Promise<{ expired: string[] }> {
  return postJson<{ expired: string[] }>("/local-api/onboarding-v2/gmail/check-tokens", { emails });
}

export interface GmailSyncWindowEstimate {
  messages: number;
  est_seconds: number;
  est_minutes: number;
  truncated?: boolean;
}

export interface GmailSyncEstimateResponse {
  status: string;
  scope_query?: string;
  windows?: string[];
  accounts?: Array<{ email: string; error?: string; windows: Record<string, GmailSyncWindowEstimate> }>;
  totals?: Record<string, GmailSyncWindowEstimate>;
  error?: string;
}

export function estimateGmailSync(
  body: { accounts?: string[]; windows?: string[] } = {}
): Promise<GmailSyncEstimateResponse> {
  return postJson<GmailSyncEstimateResponse>("/local-api/onboarding-v3/gmail/estimate", body);
}

export function fetchOnboardingV2MessagesStatus(): Promise<Record<string, unknown>> {
  return getJson<Record<string, unknown>>("/local-api/onboarding-v2/messages/status");
}

export function runOnboardingV2Messages(body: Record<string, unknown> = {}): Promise<{ job: SetupJob; status: Record<string, unknown> }> {
  return postJson<{ job: SetupJob; status: Record<string, unknown> }>("/local-api/onboarding-v2/messages/run", body);
}

export async function uploadLinkedInCsv(file: File): Promise<{ path: string }> {
  const content = await file.text();
  return postJson<{ path: string }>("/local-api/setup/linkedin-csv-upload", {
    filename: file.name,
    content,
  });
}

export function fetchOnboardingV3LinkedInStatus(): Promise<Record<string, any>> {
  return getJson<Record<string, any>>("/local-api/onboarding-v3/linkedin/status");
}

export function runOnboardingV3LinkedIn(
  body: Record<string, unknown>
): Promise<{ job: SetupJob; status: Record<string, unknown> }> {
  return postJson<{ job: SetupJob; status: Record<string, unknown> }>("/local-api/onboarding-v3/linkedin/run", body);
}

export function updateEnvKeys(
  updates: Record<string, string>
): Promise<{ written: string[]; rejected: string[]; status: EnvStatusResponse }> {
  return postJson<{ written: string[]; rejected: string[]; status: EnvStatusResponse }>(
    "/local-api/env/update",
    updates
  );
}

export type PowersetWhoami = {
  status: string;
  email: string | null;
  expired: boolean | null;
  secondsRemaining: number | null;
};

export function fetchPowersetWhoami(): Promise<PowersetWhoami> {
  return getJson<PowersetWhoami>("/local-api/powerset/whoami");
}

export function runPowersetLogin(): Promise<{ job: SetupJob }> {
  return postJson<{ job: SetupJob }>("/local-api/powerset/login", {});
}

export function fetchMessageReview(
  options: { filter?: MessageReviewFilter; query?: string; offset?: number; limit?: number } = {}
): Promise<MessageReviewResponse> {
  const params = new URLSearchParams();
  if (options.filter) params.set("filter", options.filter);
  if (options.query) params.set("q", options.query);
  if (options.offset != null) params.set("offset", String(options.offset));
  if (options.limit != null) params.set("limit", String(options.limit));
  const query = params.toString() ? `?${params.toString()}` : "";
  return getJson<MessageReviewResponse>(`/local-api/messages/review${query}`);
}

export function toggleMessageReviewRow(index: number, selected: boolean): Promise<MessageReviewResponse> {
  return postJson<MessageReviewResponse>("/local-api/messages/review/toggle", { row: index, selected });
}

export function saveMessageReviewHint(index: number, hint: string): Promise<MessageReviewResponse> {
  return postJson<MessageReviewResponse>("/local-api/messages/review/hint", { row: index, hint });
}

export function bulkToggleMessageReview(tab: "in_network", selected: boolean): Promise<MessageReviewResponse> {
  return postJson<MessageReviewResponse>("/local-api/messages/review/bulk-toggle", { tab, selected });
}
