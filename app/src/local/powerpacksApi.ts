import type { LocalRunResultsResponse, LocalRunSummary } from "./types";

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
