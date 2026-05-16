import type { CompanyDetailParams, CompanyDetailResponse, CompanySearchParams, CompanySearchResponse, ContactsV2Params, ContactsV2Result, LocalRunResultsResponse, LocalRunSummary } from "./types";

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


export function fetchContacts(options: ContactsV2Params = {}): Promise<ContactsV2Result> {
  const params = new URLSearchParams();
  if (options.page != null) params.set("page", String(options.page));
  if (options.pageSize != null) params.set("page_size", String(options.pageSize));
  if (options.search) params.set("search", options.search);
  if (options.sortField) params.set("sort_field", options.sortField);
  if (options.sortDir) params.set("sort_dir", options.sortDir);
  const query = params.toString() ? `?${params.toString()}` : "";
  return getJson<ContactsV2Result>(`/local-api/contacts${query}`);
}

function appendArrayParam(params: URLSearchParams, key: string, values?: string[]) {
  for (const value of values || []) {
    if (value.trim()) params.append(key, value.trim());
  }
}

export function fetchCompanies(options: CompanySearchParams = {}): Promise<CompanySearchResponse> {
  const params = new URLSearchParams();
  if (options.name) params.set("name", options.name);
  appendArrayParam(params, "sector_types", options.sector_types);
  appendArrayParam(params, "entity_types", options.entity_types);
  if (options.limit != null) params.set("limit", String(options.limit));
  if (options.offset != null) params.set("offset", String(options.offset));
  const query = params.toString() ? `?${params.toString()}` : "";
  return getJson<CompanySearchResponse>(`/local-api/companies${query}`);
}

export function fetchCompanyDetail(id: string, options: CompanyDetailParams = {}): Promise<CompanyDetailResponse> {
  const params = new URLSearchParams();
  if (options.include_people != null) params.set("include_people", String(options.include_people));
  if (options.people_limit != null) params.set("people_limit", String(options.people_limit));
  if (options.people_offset != null) params.set("people_offset", String(options.people_offset));
  if (options.people_sort) params.set("people_sort", options.people_sort);
  if (options.people_dir) params.set("people_dir", options.people_dir);
  if (options.people_search) params.set("people_search", options.people_search);
  const query = params.toString() ? `?${params.toString()}` : "";
  return getJson<CompanyDetailResponse>(`/local-api/companies/${encodeURIComponent(id)}${query}`);
}
