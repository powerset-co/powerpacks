// API client for the GET /local-api/contacts endpoint. Do not move these
// helpers into powerpacksApi.ts.

export type ContactsSortField =
  | "total_interactions"
  | "first_name"
  | "last_name"
  | "headline"
  | "current_company"
  | "city";
export type ContactsSortDir = "asc" | "desc";

export interface LocalContactRow {
  person_id: string;
  first_name: string | null;
  last_name: string | null;
  full_name: string | null;
  headline: string | null;
  current_title: string | null;
  current_company: string | null;
  city: string | null;
  state: string | null;
  country: string | null;
  location_raw: string | null;
  primary_email: string | null;
  all_emails: string[] | null;
  primary_phone: string | null;
  all_phones: string[] | null;
  source_channels: string[] | null;
  x_twitter_handle: string | null;
  twitter_handle: string | null;
  public_identifier: string | null;
  public_profile_url: string | null;
  linkedin_url: string | null;
  profile_picture_url: string | null;
  total_interactions?: number;
}

export interface LocalContactsResponse {
  rows: LocalContactRow[];
  total: number;
  page: number;
  page_size: number;
  index_missing?: boolean;
  interactions_available?: boolean;
}

export interface FetchLocalContactsOptions {
  q?: string;
  sort?: ContactsSortField;
  dir?: ContactsSortDir;
  page?: number;
  pageSize?: number;
}

export async function fetchLocalContacts(options: FetchLocalContactsOptions = {}): Promise<LocalContactsResponse> {
  const params = new URLSearchParams();
  if (options.q) params.set("q", options.q);
  if (options.sort) params.set("sort", options.sort);
  if (options.dir) params.set("dir", options.dir);
  if (options.page != null && options.page > 0) params.set("page", String(options.page));
  if (options.pageSize != null) params.set("page_size", String(options.pageSize));
  const query = params.toString() ? `?${params.toString()}` : "";

  const response = await fetch(`/local-api/contacts${query}`);
  if (!response.ok) {
    const text = await response.text().catch(() => "");
    throw new Error(`/local-api/contacts failed: ${response.status} ${text}`);
  }
  return response.json() as Promise<LocalContactsResponse>;
}
