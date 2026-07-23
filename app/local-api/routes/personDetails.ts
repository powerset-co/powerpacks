import path from "path";
import fs from "fs";

import { sendJson } from "../lib/http";
import { queryLocalDuckdb } from "../lib/duckdb";
import { powerpacksRepoRoot } from "../lib/paths";

// GET /local-api/contacts/person/:person_id
//
// Returns { profile, positions, education, summary } for one person from the
// local DuckDB search index. Profile JSON-ish fields are normalized to arrays,
// positions are sorted current-first then most-recent-first, education is
// sorted by most recent year.

function localDuckdbPath(): string {
  return path.join(powerpacksRepoRoot, ".powerpacks", "search-index", "local-search.duckdb");
}

function asString(value: unknown): string {
  if (value == null) return "";
  return String(value).trim();
}

function asStringOrNull(value: unknown): string | null {
  const text = asString(value);
  return text ? text : null;
}

// Normalize array-ish columns. DuckDB list columns arrive as real JSON arrays,
// but stay defensive about JSON-encoded strings ('["a","b"]') and plain
// delimited strings from older index builds.
function asStringArray(value: unknown): string[] {
  if (value == null) return [];
  if (Array.isArray(value)) {
    return value.map((item) => asString(item)).filter(Boolean);
  }
  const text = asString(value);
  if (!text) return [];
  if (text.startsWith("[")) {
    try {
      const parsed = JSON.parse(text);
      if (Array.isArray(parsed)) return parsed.map((item) => asString(item)).filter(Boolean);
    } catch {
      // fall through to delimiter split
    }
  }
  return text
    .split(/[;,]/)
    .map((item) => item.trim())
    .filter(Boolean);
}

// DuckDB JSON columns (e.g. linkedin_followers) arrive as JSON-encoded strings
// or null; normalize to a number when one is present.
function asNumberOrNull(value: unknown): number | null {
  if (value == null) return null;
  if (typeof value === "number") return Number.isFinite(value) ? value : null;
  const text = asString(value).replace(/^"|"$/g, "");
  if (!text) return null;
  const parsed = Number(text);
  return Number.isFinite(parsed) ? parsed : null;
}

// Position epochs are seconds; the index uses 0 as "missing".
function asEpochOrNull(value: unknown): number | null {
  const parsed = asNumberOrNull(value);
  return parsed && parsed > 0 ? parsed : null;
}

// Education years use 0 as "missing".
function asYearOrNull(value: unknown): number | null {
  const parsed = asNumberOrNull(value);
  return parsed && parsed > 0 ? Math.trunc(parsed) : null;
}

function asBoolean(value: unknown): boolean {
  return value === true || value === "true" || value === 1;
}

// The stored summary blob is a composite built by the indexing pack
// (packs/indexing/lib/artifacts.py:_summary_text): prose lines joined with
// "\n" followed by optional trailer lines that start with "Experience: " and
// "Education: " (semicolon-joined recaps of positions/schools). The page has
// dedicated Experience and Education sections, so strip those trailer lines
// and return only the clean prose part. Trailer detection is line-anchored
// ("Education: " at the start of a line) so prose that merely mentions the
// words is untouched.
// Some upstream people.csv rows carry machine review metadata instead of a
// prose summary. The messages-import flow
// (packs/ingestion/primitives/discover_contacts_pipeline/messages/discover.py,
// review_row_to_messages_people) writes summary as "; "-joined key=value
// pairs, e.g.:
//   messages_total=0; selection=in_network; review_reason=Raw contact review fallback
// It is never prose, so drop any line made up entirely of these known
// key=value segments. Requiring 2+ segments (the writer always emits at least
// messages_total and selection) keeps prose that happens to contain a single
// "key=value" token safe.
const REVIEW_METADATA_SEGMENT = /^(messages_total|selection|last_message|review_reason)=/;

function isReviewMetadataLine(line: string): boolean {
  const segments = line
    .split(";")
    .map((segment) => segment.trim())
    .filter(Boolean);
  return segments.length >= 2 && segments.every((segment) => REVIEW_METADATA_SEGMENT.test(segment));
}

function stripSummaryTrailers(value: string | null): string | null {
  if (!value) return null;
  const kept = value
    .split("\n")
    .filter((line) => !/^(Education|Experience): /.test(line.trim()) && !isReviewMetadataLine(line));
  const text = kept.join("\n").trim();
  return text ? text : null;
}

export async function handlePersonDetailsRoutes(req: any, res: any, url: URL): Promise<boolean> {
  const personMatch = url.pathname.match(/^\/local-api\/contacts\/person\/([^/]+)$/);
  if (personMatch && req.method === "GET") {
    const personId = decodeURIComponent(personMatch[1]).trim().toLowerCase();
    if (!personId) {
      sendJson(res, { error: "person not found" }, 404);
      return true;
    }

    const duckdbPath = localDuckdbPath();
    if (!fs.existsSync(duckdbPath)) {
      sendJson(res, { error: "local search index not found", path: duckdbPath }, 503);
      return true;
    }

    const profileRows = queryLocalDuckdb(
      powerpacksRepoRoot,
      `select cast(person_id as varchar) as person_id, full_name, first_name, last_name, headline,
              current_title, current_company, city, state, country, location_raw,
              all_emails, all_phones, primary_email, primary_phone,
              linkedin_url, x_twitter_handle, twitter_handle, profile_picture_url,
              source_channels, summary, linkedin_followers, linkedin_connections, x_twitter_followers
       from local_person_profiles
       where cast(person_id as varchar) = ?
       limit 1`,
      [personId]
    );
    if (profileRows.length === 0) {
      sendJson(res, { error: "person not found" }, 404);
      return true;
    }

    const raw = profileRows[0];
    const profile = {
      person_id: asString(raw.person_id),
      full_name: asString(raw.full_name),
      first_name: asString(raw.first_name),
      last_name: asString(raw.last_name),
      headline: asStringOrNull(raw.headline),
      current_title: asStringOrNull(raw.current_title),
      current_company: asStringOrNull(raw.current_company),
      city: asStringOrNull(raw.city),
      state: asStringOrNull(raw.state),
      country: asStringOrNull(raw.country),
      location_raw: asStringOrNull(raw.location_raw),
      all_emails: asStringArray(raw.all_emails),
      all_phones: asStringArray(raw.all_phones),
      primary_email: asStringOrNull(raw.primary_email),
      primary_phone: asStringOrNull(raw.primary_phone),
      linkedin_url: asStringOrNull(raw.linkedin_url),
      x_twitter_handle: asStringOrNull(raw.x_twitter_handle) || asStringOrNull(raw.twitter_handle),
      profile_picture_url: asStringOrNull(raw.profile_picture_url),
      source_channels: asStringArray(raw.source_channels),
      summary: stripSummaryTrailers(asStringOrNull(raw.summary)),
      linkedin_followers: asNumberOrNull(raw.linkedin_followers),
      linkedin_connections: asNumberOrNull(raw.linkedin_connections),
      x_twitter_followers: asNumberOrNull(raw.x_twitter_followers),
    };

    const positionRows = queryLocalDuckdb(
      powerpacksRepoRoot,
      `select cast(pos.position_id as varchar) as position_id,
              pos.position_title, pos.raw_title,
              cast(pos.company_id as varchar) as company_id,
              pos.company_name, pos.company_domain, pos.company_linkedin_url,
              pos.description, pos.is_current, pos.start_date_epoch, pos.end_date_epoch,
              pos.city, pos.state, pos.country, pos.seniority_band, pos.role_ids, pos.tenure_years,
              c.company_name as joined_company_name,
              c.website_domain as joined_company_domain,
              c.linkedin_url as joined_company_linkedin_url,
              c.logo_url as company_logo_url
       from local_people_positions pos
       left join local_companies c on c.id = pos.company_id
       where cast(pos.person_id as varchar) = ?
       order by coalesce(pos.is_current, false) desc,
                coalesce(pos.start_date_epoch, 0) desc`,
      [personId]
    );
    const positions = positionRows.map((row) => ({
      position_id: asString(row.position_id),
      position_title: asStringOrNull(row.position_title) || asStringOrNull(row.raw_title),
      raw_title: asStringOrNull(row.raw_title),
      company_id: asStringOrNull(row.company_id),
      company_name: asStringOrNull(row.company_name) || asStringOrNull(row.joined_company_name),
      company_domain: asStringOrNull(row.company_domain) || asStringOrNull(row.joined_company_domain),
      company_linkedin_url:
        asStringOrNull(row.company_linkedin_url) || asStringOrNull(row.joined_company_linkedin_url),
      company_logo_url: asStringOrNull(row.company_logo_url),
      description: asStringOrNull(row.description),
      is_current: asBoolean(row.is_current),
      start_date_epoch: asEpochOrNull(row.start_date_epoch),
      end_date_epoch: asEpochOrNull(row.end_date_epoch),
      city: asStringOrNull(row.city),
      state: asStringOrNull(row.state),
      country: asStringOrNull(row.country),
      seniority_band: asStringOrNull(row.seniority_band),
      role_ids: asStringArray(row.role_ids),
      tenure_years: asNumberOrNull(row.tenure_years),
    }));

    const educationRows = queryLocalDuckdb(
      powerpacksRepoRoot,
      `select school_name, degree, degree_normalized, field_of_study,
              start_year, end_year, graduation_year
       from local_people_education
       where cast(person_id as varchar) = ?
       order by coalesce(nullif(end_year, 0), nullif(graduation_year, 0), nullif(start_year, 0), 0) desc`,
      [personId]
    );
    const education = educationRows.map((row) => ({
      school_name: asStringOrNull(row.school_name),
      degree: asStringOrNull(row.degree),
      degree_normalized: asStringOrNull(row.degree_normalized),
      field_of_study: asStringOrNull(row.field_of_study),
      start_year: asYearOrNull(row.start_year),
      end_year: asYearOrNull(row.end_year),
      graduation_year: asYearOrNull(row.graduation_year),
    }));

    // tech_skills is intentionally not selected: local_summaries.tech_skills
    // holds raw RapidAPI skill/endorsement dicts and the page no longer
    // renders skill chips or endorsement content.
    const summaryRows = queryLocalDuckdb(
      powerpacksRepoRoot,
      `select summary from local_summaries where cast(person_id as varchar) = ? limit 1`,
      [personId]
    );
    const summary = summaryRows.length
      ? {
          summary: stripSummaryTrailers(asStringOrNull(summaryRows[0].summary)),
        }
      : null;

    sendJson(res, { profile, positions, education, summary });
    return true;
  }

  return false;
}
