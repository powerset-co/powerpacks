import type {
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

export function fetchSetupStatus(): Promise<SetupStatusResponse> {
  return getJson<SetupStatusResponse>("/local-api/setup/status");
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
