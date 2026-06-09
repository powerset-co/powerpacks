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
