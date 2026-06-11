// Fetch client + types for the local person-details vertical.
// Talks to GET /local-api/contacts/person/:person_id (see
// app/local-api/routes/personDetails.ts).

export interface LocalPersonProfile {
  person_id: string;
  full_name: string;
  first_name: string;
  last_name: string;
  headline: string | null;
  current_title: string | null;
  current_company: string | null;
  city: string | null;
  state: string | null;
  country: string | null;
  location_raw: string | null;
  all_emails: string[];
  all_phones: string[];
  primary_email: string | null;
  primary_phone: string | null;
  linkedin_url: string | null;
  x_twitter_handle: string | null;
  profile_picture_url: string | null;
  source_channels: string[];
  summary: string | null;
  linkedin_followers: number | null;
  linkedin_connections: number | null;
  x_twitter_followers: number | null;
}

export interface LocalPersonPosition {
  position_id: string;
  position_title: string | null;
  raw_title: string | null;
  company_id: string | null;
  company_name: string | null;
  company_domain: string | null;
  company_linkedin_url: string | null;
  company_logo_url: string | null;
  description: string | null;
  is_current: boolean;
  /** Unix epoch in seconds, or null when unknown. */
  start_date_epoch: number | null;
  /** Unix epoch in seconds, or null when unknown / present. */
  end_date_epoch: number | null;
  city: string | null;
  state: string | null;
  country: string | null;
  seniority_band: string | null;
  role_ids: string[];
  tenure_years: number | null;
}

export interface LocalPersonEducation {
  school_name: string | null;
  degree: string | null;
  degree_normalized: string | null;
  field_of_study: string | null;
  start_year: number | null;
  end_year: number | null;
  graduation_year: number | null;
}

// Clean prose only: the server strips indexer-appended Experience/Education
// trailer lines and messages-import review metadata lines
// (messages_total=...; selection=...; ...), and tech_skills (raw endorsement
// data) is no longer exposed.
export interface LocalPersonSummary {
  summary: string | null;
}

export interface LocalPersonDetailsResponse {
  profile: LocalPersonProfile;
  positions: LocalPersonPosition[];
  education: LocalPersonEducation[];
  summary: LocalPersonSummary | null;
}

/**
 * Fetch one person's full local profile. Resolves to null on 404 (person not
 * in the local index); throws on other failures.
 */
export async function fetchPersonDetails(personId: string): Promise<LocalPersonDetailsResponse | null> {
  const response = await fetch(`/local-api/contacts/person/${encodeURIComponent(personId)}`);
  if (response.status === 404) return null;
  if (!response.ok) {
    const text = await response.text().catch(() => "");
    throw new Error(`Failed to load person (${response.status}) ${text}`.trim());
  }
  return (await response.json()) as LocalPersonDetailsResponse;
}
