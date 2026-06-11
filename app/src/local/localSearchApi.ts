// API client for the local search launcher. Talks to the
// /local-api/search/local-run endpoints; do not move these helpers into
// powerpacksApi.ts.

export type LocalSearchJobStatus = "running" | "completed" | "failed" | "blocked";

export interface LocalSearchStartResponse {
  jobId: string;
  status: LocalSearchJobStatus;
  ledger: string;
}

export interface LocalSearchStepStatus {
  id: string;
  status: string;
}

export interface LocalSearchJob {
  jobId: string;
  status: LocalSearchJobStatus;
  query: string | null;
  startedAt: string;
  completedAt: string | null;
  taskId: string | null;
  conversationId: string | null;
  steps: LocalSearchStepStatus[];
  summary: Record<string, unknown> | null;
  error: string | null;
  logTail: string;
}

async function requestJson<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, init);
  const text = await response.text().catch(() => "");
  if (!response.ok) {
    let message = `${path} failed: ${response.status}`;
    try {
      const parsed = JSON.parse(text);
      if (parsed?.error) message = String(parsed.error);
    } catch {
      if (text) message = `${message} ${text.slice(0, 300)}`;
    }
    throw new Error(message);
  }
  return JSON.parse(text) as T;
}

export function startLocalSearch(query: string): Promise<LocalSearchStartResponse> {
  return requestJson<LocalSearchStartResponse>("/local-api/search/local-run", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ query }),
  });
}

export function fetchLocalSearchJob(jobId: string): Promise<LocalSearchJob> {
  return requestJson<LocalSearchJob>(`/local-api/search/local-run/${encodeURIComponent(jobId)}`);
}
