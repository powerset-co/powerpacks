import type {
  EnvStatusResponse,
  LocalProfileResponse,
  LocalRunResultsResponse,
  LocalRunSummary,
  MessageReviewFilter,
  MessageReviewResponse,
  SetupEnrichmentSource,
  SetupJob,
  SetupSourceStatus,
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

export interface SystemUpdateStatus {
  branch: string;
  current_hash: string;
  latest_hash: string;
  short_current: string;
  short_latest: string;
  behind: number;
  dirty: boolean;
  update_available: boolean;
  versions: { powerpacks: string; console: string };
  hosts: { claude: boolean; codex: boolean };
  checked_at: string;
}

export interface SystemDaemonStatus {
  daemonized: boolean;
  running: boolean;
  pid: number | null;
  port: string;
  raw: string;
}

export function fetchSystemUpdateStatus(): Promise<SystemUpdateStatus> {
  return getJson<SystemUpdateStatus>("/local-api/system/update-status");
}

export function fetchSystemDaemonStatus(): Promise<SystemDaemonStatus> {
  return getJson<SystemDaemonStatus>("/local-api/system/daemon-status");
}

export function startSystemUpdate(): Promise<{ job: SetupJob; hosts: { claude: boolean; codex: boolean }; steps: string[]; auto_restart: boolean }> {
  return postJson("/local-api/system/update", {});
}

export function restartSystem(): Promise<{ restarting: boolean; url: string; port: string }> {
  return postJson("/local-api/system/restart", {});
}

// Cheap liveness probe used by the FE to detect when the server is back after a
// self-restart. Returns false (rather than throwing) while the server is down.
export async function fetchSystemHealth(): Promise<boolean> {
  try {
    const res = await fetch("/local-api/system/health", { cache: "no-store" });
    return res.ok;
  } catch {
    return false;
  }
}

export interface SystemReadinessSecret {
  key: string;
  label: string;
  provider: string;
  satisfied: boolean;
  writable: boolean;
  fix: string;
  getUrl: string;
  optional: boolean;
}

export interface SystemReadinessCapability {
  id: string;
  label: string;
  description?: string;
  requires: string[];
  core: boolean;
  satisfied: boolean;
  missing: string[];
}

export interface SystemReadiness {
  ready: boolean;
  login: { logged_in: boolean; email: string; expires_at: number; expired: boolean };
  secrets: SystemReadinessSecret[];
  capabilities: SystemReadinessCapability[];
  checked_at: string;
}

export function fetchSystemReadiness(): Promise<SystemReadiness> {
  return getJson<SystemReadiness>("/local-api/system/readiness");
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
  return postJson<GmailSyncEstimateResponse>("/local-api/onboarding/gmail/estimate", body);
}

export interface GmailAccount {
  email: string;
  message_count: number;
  last_sync: string;
}

export interface GmailAccountsResponse {
  status: string;
  accounts: GmailAccount[];
  error?: string;
}

export function fetchGmailAccounts(): Promise<GmailAccountsResponse> {
  return getJson<GmailAccountsResponse>("/local-api/onboarding/gmail/accounts");
}

export function runGmailWindowSync(
  body: { window: string; accounts?: string[]; limit?: number }
): Promise<{ job: SetupJob }> {
  return postJson<{ job: SetupJob }>("/local-api/onboarding/gmail/sync", body);
}

export function fetchOnboardingMessagesStatus(): Promise<Record<string, unknown>> {
  return getJson<Record<string, unknown>>("/local-api/onboarding/messages/status");
}

export function runOnboardingMessages(body: Record<string, unknown> = {}): Promise<{ job: SetupJob; status: Record<string, unknown> }> {
  return postJson<{ job: SetupJob; status: Record<string, unknown> }>("/local-api/onboarding/messages/run", body);
}

export async function uploadLinkedInCsv(file: File): Promise<{ path: string }> {
  const content = await file.text();
  return postJson<{ path: string }>("/local-api/setup/linkedin-csv-upload", {
    filename: file.name,
    content,
  });
}

export function fetchOnboardingLinkedInStatus(): Promise<Record<string, any>> {
  return getJson<Record<string, any>>("/local-api/onboarding/linkedin/status");
}

export interface LinkedInSourceStatusResponse {
  source: SetupSourceStatus;
  discovery: {
    status: string;
    connections: number;
    parsed: number;
    skippedInvalid: number;
    updatedAt?: string | null;
    artifactDir: string;
    sourceCsv: string;
    sourceCsvExists: boolean;
    sourceCsvSizeBytes: number;
  };
  enrichment: SetupEnrichmentSource;
}

export function fetchLinkedInSourceStatus(): Promise<LinkedInSourceStatusResponse> {
  return getJson<LinkedInSourceStatusResponse>("/local-api/sources/linkedin/status");
}

export interface GmailSourceStatusResponse {
  source: SetupSourceStatus;
  discovery: {
    status: string;
    contacts: number;
    accounts: Array<{ email: string; contacts: number; status: string }>;
    updatedAt?: string | null;
    artifactDir: string;
  };
  enrichment: SetupEnrichmentSource;
}

export function fetchGmailSourceStatus(): Promise<GmailSourceStatusResponse> {
  return getJson<GmailSourceStatusResponse>("/local-api/sources/gmail/status");
}

export function runOnboardingLinkedIn(
  body: Record<string, unknown>
): Promise<{ job: SetupJob; status: Record<string, unknown> }> {
  return postJson<{ job: SetupJob; status: Record<string, unknown> }>("/local-api/onboarding/linkedin/run", body);
}

export function fetchOnboardingGmailRunStatus(): Promise<Record<string, any>> {
  return getJson<Record<string, any>>("/local-api/onboarding/gmail/run-status");
}

// Free, instant incremental Parallel.ai spend estimate for the next Gmail Process.
export function fetchGmailEnrichEstimate(): Promise<Record<string, any>> {
  return getJson<Record<string, any>>("/local-api/onboarding/gmail/enrich-estimate");
}

// Gmail "Process": local Parallel.ai enrich -> Modal index-only. Body is empty;
// the backend resolves the operator and the merged people.csv path itself.
export function runOnboardingGmail(
  body: Record<string, unknown> = {}
): Promise<{ job: SetupJob; status: Record<string, unknown> }> {
  return postJson<{ job: SetupJob; status: Record<string, unknown> }>("/local-api/onboarding/gmail/run", body);
}

// msgvault setup state: gcloud auth, OAuth app (client_secret), db, authorized accounts.
export interface MsgvaultStatus {
  status: string; // "ok" | "needs_setup" | "error"
  owner_email?: string; // primary account from setup state (seeds the authorize list)
  desired_emails?: string[]; // emails the user asked to authorize (test_users) — source of truth pre-auth
  accounts: Array<{ email?: string; message_count?: number; last_sync?: string | null }>; // authorized (msgvault rows)
  config?: { oauth_configured?: boolean; exists?: boolean };
  database?: { exists?: boolean };
  gcloud?: { installed?: boolean; account?: string; project?: string };
  msgvault?: { installed?: boolean };
  error?: string;
}

export function fetchMsgvaultStatus(): Promise<MsgvaultStatus> {
  return getJson<MsgvaultStatus>("/local-api/onboarding/gmail/msgvault-status");
}

// One-shot: create gcloud project + OAuth app + add all emails as test users
// (no authorization). Returns a job to poll.
export function runGmailVaultSetup(
  body: { primaryEmail: string; additionalEmails?: string[] }
): Promise<{ job: SetupJob }> {
  return postJson<{ job: SetupJob }>("/local-api/onboarding/gmail/vault-setup", body);
}

// Authorize one Gmail account (per-account browser grant). Returns a job to poll.
export function runGmailAuthorize(body: { email: string }): Promise<{ job: SetupJob }> {
  return postJson<{ job: SetupJob }>("/local-api/onboarding/gmail/authorize", body);
}

// Link an uploaded Connections.csv (write csv_path + linked) without running the
// import/enrich/index pipeline — enrich/index stay behind their own buttons.
export function linkLinkedInCsv(
  body: { csvPath: string; sourceLabel?: string }
): Promise<{ status: string; linked?: boolean; csv?: string; error?: string }> {
  return postJson<{ status: string; linked?: boolean; csv?: string; error?: string }>(
    "/local-api/onboarding/linkedin/link",
    body
  );
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

export function runPowersetPullKeys(): Promise<{ job: SetupJob }> {
  return postJson<{ job: SetupJob }>("/local-api/powerset/pull-keys", {});
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

export type { SetupJob } from "./types";
