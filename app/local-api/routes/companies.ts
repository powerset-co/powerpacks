import path from "path";
import fs from "fs";

import { sendJson } from "../lib/http";
import { queryLocalDuckdb } from "../lib/duckdb";
import { powerpacksRepoRoot } from "../lib/paths";

// Company directory routes backed by the local DuckDB search index.
//
// GET /local-api/companies                → paginated directory with people counts
// GET /local-api/companies/:company_id    → { company, people }

function localDuckdbPath(): string {
  return path.join(powerpacksRepoRoot, ".powerpacks", "search-index", "local-search.duckdb");
}

// ── Search parser ────────────────────────────────────────────────────
// Same parser style as routes/contacts.ts `parseContactSearch`.
// Plain words → company name/alias/domain search. Prefix values consume all
// text until the next prefix. Supported prefixes: sector:, city:.
const FIELD_PREFIXES = ["sector:", "city:"];
const PREFIX_SPLIT_PATTERN = /(?=(?:sector:|city:))/;

type ParsedTerm = { field: string; value: string };

function parseCompanySearch(raw: string): ParsedTerm[] {
  const segments = raw.split(PREFIX_SPLIT_PATTERN);
  const results: ParsedTerm[] = [];
  for (const rawSeg of segments) {
    const seg = rawSeg.trim();
    if (!seg) continue;

    let field = "name";
    let value = seg;
    for (const prefix of FIELD_PREFIXES) {
      if (seg.toLowerCase().startsWith(prefix)) {
        field = prefix.slice(0, -1);
        value = seg.slice(prefix.length).trim();
        break;
      }
    }

    if (value) results.push({ field, value });
  }
  return results;
}

function buildSearchClause(parsed: ParsedTerm[]): { clause: string; params: unknown[] } {
  const conditions: string[] = [];
  const params: unknown[] = [];

  for (const { field, value } of parsed) {
    const lowered = value.toLowerCase();

    if (field === "sector") {
      conditions.push("lower(coalesce(array_to_string(c.sector_types, ' '), '')) LIKE '%' || ? || '%'");
      params.push(lowered);
    } else if (field === "city") {
      conditions.push(
        "strip_accents(lower(coalesce(c.city, '') || ' ' || coalesce(c.state, '') || ' ' || coalesce(c.metro_area, ''))) LIKE '%' || strip_accents(?) || '%'"
      );
      params.push(lowered);
    } else {
      // "name" — default. Matches company name, aliases, and website domain.
      conditions.push(
        "strip_accents(lower(coalesce(c.company_name, '') || ' ' || coalesce(array_to_string(c.aliases, ' '), '') || ' ' || coalesce(c.website_domain, ''))) LIKE '%' || strip_accents(?) || '%'"
      );
      params.push(lowered);
    }
  }

  return {
    clause: conditions.length > 0 ? `WHERE ${conditions.join(" AND ")}` : "",
    params,
  };
}

// Safe sort expression mapping (prevents SQL injection via dynamic sort).
// Numeric sorts coalesce 0 (the index's "missing" marker) so direction flips
// behave; deterministic tiebreakers are appended in the ORDER BY below.
const COMPANY_SORT_MAP: Record<string, string> = {
  current_people: "coalesce(p.current_people, 0)",
  total_people: "coalesce(p.total_people, 0)",
  company_name: "lower(coalesce(c.company_name, ''))",
  headcount: "coalesce(c.headcount, 0)",
  founded_year: "coalesce(c.founded_year, 0)",
};

const DEFAULT_SORT = "current_people";

function defaultDirFor(sort: string): "DESC" | "ASC" {
  return sort === "company_name" ? "ASC" : "DESC";
}

// People-count aggregate joined onto companies. CASE yields NULL for past
// positions so COUNT(DISTINCT ...) only counts current people.
const PEOPLE_COUNTS_SUBQUERY = `
  SELECT company_id,
         count(distinct CASE WHEN coalesce(is_current, false) THEN person_id END) AS current_people,
         count(distinct person_id) AS total_people
  FROM local_people_positions
  GROUP BY company_id
`;

const COMPANY_ROW_COLUMNS = `
  cast(c.id as varchar) AS id,
  c.company_name, c.aliases, c.description, c.website_domain, c.linkedin_url, c.logo_url,
  c.city, c.state, c.country, c.metro_area,
  c.entity_types, c.sector_types, c.customer_type,
  c.headcount, c.funding_stage, c.funding_total, c.stage, c.founded_year
`;

// ── Normalizers (same conventions as routes/personDetails.ts) ────────
function asString(value: unknown): string {
  if (value == null) return "";
  return String(value).trim();
}

function asStringOrNull(value: unknown): string | null {
  const text = asString(value);
  return text ? text : null;
}

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

function asNumberOrNull(value: unknown): number | null {
  if (value == null) return null;
  if (typeof value === "number") return Number.isFinite(value) ? value : null;
  const text = asString(value).replace(/^"|"$/g, "");
  if (!text) return null;
  const parsed = Number(text);
  return Number.isFinite(parsed) ? parsed : null;
}

// The index uses 0 as "missing" for numeric company facts and position epochs.
function asPositiveNumberOrNull(value: unknown): number | null {
  const parsed = asNumberOrNull(value);
  return parsed && parsed > 0 ? parsed : null;
}

function asBoolean(value: unknown): boolean {
  return value === true || value === "true" || value === 1;
}

function asCount(value: unknown): number {
  const parsed = asNumberOrNull(value);
  return parsed && parsed > 0 ? Math.trunc(parsed) : 0;
}

function normalizeCompanyRow(row: Record<string, any>): Record<string, any> {
  return {
    id: asString(row.id),
    company_name: asString(row.company_name),
    aliases: asStringArray(row.aliases),
    description: asStringOrNull(row.description),
    website_domain: asStringOrNull(row.website_domain),
    linkedin_url: asStringOrNull(row.linkedin_url),
    logo_url: asStringOrNull(row.logo_url),
    city: asStringOrNull(row.city),
    state: asStringOrNull(row.state),
    country: asStringOrNull(row.country),
    metro_area: asStringOrNull(row.metro_area),
    entity_types: asStringArray(row.entity_types),
    sector_types: asStringArray(row.sector_types),
    customer_type: asStringArray(row.customer_type),
    headcount: asPositiveNumberOrNull(row.headcount),
    funding_stage: asPositiveNumberOrNull(row.funding_stage),
    funding_total: asPositiveNumberOrNull(row.funding_total),
    stage: asStringOrNull(row.stage),
    founded_year: asPositiveNumberOrNull(row.founded_year),
  };
}

function clampInt(raw: string | null, fallback: number, min: number, max: number): number {
  const parsed = parseInt(raw || "", 10);
  if (!Number.isFinite(parsed)) return fallback;
  return Math.min(max, Math.max(min, parsed));
}

export async function handleCompaniesRoutes(req: any, res: any, url: URL): Promise<boolean> {
  // ── GET /local-api/companies ───────────────────────────────────────
  if (url.pathname === "/local-api/companies" && req.method === "GET") {
    const q = (url.searchParams.get("q") || "").trim();
    const sortParam = url.searchParams.get("sort") || DEFAULT_SORT;
    const sort = Object.prototype.hasOwnProperty.call(COMPANY_SORT_MAP, sortParam) ? sortParam : DEFAULT_SORT;
    const dir = (url.searchParams.get("dir") || defaultDirFor(sort)).toLowerCase() === "asc" ? "ASC" : "DESC";
    const page = clampInt(url.searchParams.get("page"), 0, 0, 1_000_000);
    const pageSize = clampInt(url.searchParams.get("page_size"), 50, 1, 200);

    if (!fs.existsSync(localDuckdbPath())) {
      sendJson(res, { rows: [], total: 0, page, page_size: pageSize, index_missing: true });
      return true;
    }

    // Mirrors the contacts route: searches shorter than 2 chars are ignored.
    const parsed = q.length >= 2 ? parseCompanySearch(q) : [];
    const { clause, params } = buildSearchClause(parsed);

    const countRows = queryLocalDuckdb(
      powerpacksRepoRoot,
      `SELECT count(*) AS total FROM local_companies c ${clause}`,
      params
    );
    const total = Number(countRows[0]?.total || 0);

    // Secondary keys keep pagination deterministic when the sort key ties.
    const orderBy = `${COMPANY_SORT_MAP[sort]} ${dir}, lower(coalesce(c.company_name, '')) ASC, cast(c.id as varchar) ASC`;
    const rawRows = queryLocalDuckdb(
      powerpacksRepoRoot,
      `SELECT ${COMPANY_ROW_COLUMNS},
              coalesce(p.current_people, 0) AS current_people,
              coalesce(p.total_people, 0) AS total_people
       FROM local_companies c
       LEFT JOIN (${PEOPLE_COUNTS_SUBQUERY}) p ON p.company_id = c.id
       ${clause}
       ORDER BY ${orderBy}
       LIMIT ? OFFSET ?`,
      [...params, pageSize, page * pageSize]
    );
    const rows = rawRows.map((row) => ({
      ...normalizeCompanyRow(row),
      current_people: asCount(row.current_people),
      total_people: asCount(row.total_people),
    }));

    sendJson(res, { rows, total, page, page_size: pageSize });
    return true;
  }

  // ── GET /local-api/companies/:company_id ───────────────────────────
  const companyMatch = url.pathname.match(/^\/local-api\/companies\/([^/]+)$/);
  if (companyMatch && req.method === "GET") {
    const companyId = decodeURIComponent(companyMatch[1]).trim().toLowerCase();
    if (!companyId) {
      sendJson(res, { error: "company not found" }, 404);
      return true;
    }

    const duckdbPath = localDuckdbPath();
    if (!fs.existsSync(duckdbPath)) {
      sendJson(res, { error: "local search index not found", path: duckdbPath }, 503);
      return true;
    }

    const companyRows = queryLocalDuckdb(
      powerpacksRepoRoot,
      `SELECT ${COMPANY_ROW_COLUMNS},
              coalesce(p.current_people, 0) AS current_people,
              coalesce(p.total_people, 0) AS total_people
       FROM local_companies c
       LEFT JOIN (${PEOPLE_COUNTS_SUBQUERY}) p ON p.company_id = c.id
       WHERE cast(c.id as varchar) = ?
       LIMIT 1`,
      [companyId]
    );
    if (companyRows.length === 0) {
      sendJson(res, { error: "company not found" }, 404);
      return true;
    }
    const company = {
      ...normalizeCompanyRow(companyRows[0]),
      current_people: asCount(companyRows[0].current_people),
      total_people: asCount(companyRows[0].total_people),
    };

    // One row per (person, position) at this company; the UI groups
    // current vs past. Profiles are a LEFT JOIN — positions can reference
    // people without a local profile row, who render with id-only fallbacks.
    const peopleRows = queryLocalDuckdb(
      powerpacksRepoRoot,
      `SELECT cast(pos.person_id as varchar) AS person_id,
              prof.full_name, prof.headline, prof.profile_picture_url,
              prof.city, prof.state, prof.linkedin_url,
              pos.position_title, pos.raw_title, pos.is_current,
              pos.start_date_epoch, pos.end_date_epoch, pos.seniority_band
       FROM local_people_positions pos
       LEFT JOIN local_person_profiles prof
         ON cast(prof.person_id as varchar) = cast(pos.person_id as varchar)
       WHERE cast(pos.company_id as varchar) = ?
       ORDER BY coalesce(pos.is_current, false) DESC,
                coalesce(pos.start_date_epoch, 0) DESC,
                lower(coalesce(prof.full_name, '')) ASC,
                cast(pos.person_id as varchar) ASC`,
      [companyId]
    );
    const people = peopleRows.map((row) => ({
      person_id: asString(row.person_id),
      full_name: asStringOrNull(row.full_name),
      headline: asStringOrNull(row.headline),
      profile_picture_url: asStringOrNull(row.profile_picture_url),
      city: asStringOrNull(row.city),
      state: asStringOrNull(row.state),
      linkedin_url: asStringOrNull(row.linkedin_url),
      position_title: asStringOrNull(row.position_title) || asStringOrNull(row.raw_title),
      is_current: asBoolean(row.is_current),
      start_date_epoch: asPositiveNumberOrNull(row.start_date_epoch),
      end_date_epoch: asPositiveNumberOrNull(row.end_date_epoch),
      seniority_band: asStringOrNull(row.seniority_band),
    }));

    sendJson(res, { company, people });
    return true;
  }

  return false;
}
