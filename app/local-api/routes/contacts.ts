import fs from "fs";
import os from "os";
import path from "path";
import { spawnSync } from "child_process";

import { sendJson } from "../lib/http";
import { queryLocalDuckdb } from "../lib/duckdb";
import { powerpacksRepoRoot } from "../lib/paths";
import { setupProcessEnv } from "../lib/env";

// ── Search parser ────────────────────────────────────────────────────
// Mirrors network-search-api api_v2/routes/contacts.py `_parse_contact_search`.
// Supports: headline:engineer, email:@stripe, company:the black tux, phone:408
// plus local additions twitter:<handle> and city:<text>.
// Plain words → name search. Prefix values consume all text until the next prefix.
// `phone:` / `twitter:` with empty value are "has any" flags.
const FIELD_PREFIXES = ["headline:", "email:", "company:", "phone:", "twitter:", "city:"];
const PREFIX_SPLIT_PATTERN = /(?=(?:headline:|email:|company:|phone:|twitter:|city:))/;

type ParsedTerm = { field: string; value: string };

function parseContactSearch(raw: string): ParsedTerm[] {
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

    if (field === "phone" || field === "twitter") {
      // empty value means "has any phone" / "has any twitter handle"
      results.push({ field, value });
    } else if (value) {
      results.push({ field, value });
    }
  }
  return results;
}

// Digit-only blob across all phone fields (mirrors prod phone_blob regexp_replace).
const PHONE_BLOB_SQL =
  "regexp_replace(coalesce(array_to_string(all_phones, ' '), '') || ' ' || coalesce(primary_phone, ''), '[^0-9]+', '', 'g')";

function buildSearchClause(parsed: ParsedTerm[]): { clause: string; params: unknown[] } {
  const conditions: string[] = [];
  const params: unknown[] = [];

  for (const { field, value } of parsed) {
    if (field === "phone") {
      const digits = (value || "").replace(/\D+/g, "");
      if (!digits) {
        conditions.push(`nullif(${PHONE_BLOB_SQL}, '') IS NOT NULL`);
        continue;
      }
      conditions.push(`${PHONE_BLOB_SQL} LIKE '%' || ? || '%'`);
      params.push(digits);
      continue;
    }

    if (field === "twitter") {
      const handle = (value || "").replace(/^@/, "").toLowerCase();
      if (!handle) {
        conditions.push("trim(coalesce(x_twitter_handle, '') || coalesce(twitter_handle, '')) != ''");
        continue;
      }
      conditions.push(
        "lower(coalesce(x_twitter_handle, '') || ' ' || coalesce(twitter_handle, '')) LIKE '%' || ? || '%'"
      );
      params.push(handle);
      continue;
    }

    if (!value) continue;
    const lowered = value.toLowerCase();

    if (field === "headline") {
      conditions.push(
        "strip_accents(lower(coalesce(headline, '') || ' ' || coalesce(current_title, ''))) LIKE '%' || strip_accents(?) || '%'"
      );
      params.push(lowered);
    } else if (field === "email") {
      conditions.push(
        "lower(coalesce(array_to_string(all_emails, ' '), '') || ' ' || coalesce(primary_email, '')) LIKE '%' || ? || '%'"
      );
      params.push(lowered);
    } else if (field === "company") {
      conditions.push("strip_accents(lower(coalesce(current_company, ''))) LIKE '%' || strip_accents(?) || '%'");
      params.push(lowered);
    } else if (field === "city") {
      conditions.push(
        "strip_accents(lower(coalesce(city, '') || ' ' || coalesce(location_raw, ''))) LIKE '%' || strip_accents(?) || '%'"
      );
      params.push(lowered);
    } else {
      // "name" — default. Prod matches first_name + last_name; full_name is a
      // local addition so single-field names still match.
      conditions.push(
        "strip_accents(lower(coalesce(first_name, '') || ' ' || coalesce(last_name, '') || ' ' || coalesce(full_name, ''))) LIKE '%' || strip_accents(?) || '%'"
      );
      params.push(lowered);
    }
  }

  return {
    clause: conditions.length > 0 ? `WHERE ${conditions.join(" AND ")}` : "",
    params,
  };
}

// ── Interaction counts ───────────────────────────────────────────────
// Gmail interaction counts per email address, aggregated from the msgvault
// metadata store (same canonical path as discover_contacts_pipeline's
// DEFAULT_MSGVAULT_DB). Metadata only — message bodies are never read.
const MSGVAULT_DB_PATH = path.join(os.homedir(), ".msgvault", "msgvault.db");
const INTERACTION_CACHE_TTL_MS = 60000;

const MSGVAULT_COUNTS_SCRIPT = `
import sqlite3, json, sys
payload = json.load(sys.stdin)
con = sqlite3.connect(f"file:{payload['db']}?mode=ro", uri=True)
rows = con.execute("""
WITH msg_party AS (
  SELECT m.id AS message_id, m.sender_id AS participant_id FROM messages m WHERE m.sender_id IS NOT NULL
  UNION
  SELECT r.message_id, r.participant_id FROM message_recipients r
)
SELECT lower(p.email_address) AS email, COUNT(DISTINCT mp.message_id) AS cnt
FROM msg_party mp JOIN participants p ON p.id = mp.participant_id
WHERE p.email_address IS NOT NULL AND p.email_address != ''
GROUP BY 1
""").fetchall()
print(json.dumps({email: cnt for email, cnt in rows}))
`;

let interactionCountsCache: { key: string; expiresAt: number; value: Record<string, number> } | null = null;

function msgvaultInteractionCounts(): Record<string, number> | null {
  if (!fs.existsSync(MSGVAULT_DB_PATH)) return null;
  const stat = fs.statSync(MSGVAULT_DB_PATH);
  const key = `${MSGVAULT_DB_PATH}:${stat.mtimeMs}:${stat.size}`;
  const nowMs = Date.now();
  if (interactionCountsCache && interactionCountsCache.key === key && interactionCountsCache.expiresAt > nowMs) {
    return interactionCountsCache.value;
  }
  const result = spawnSync("uv", ["run", "--project", ".", "python", "-c", MSGVAULT_COUNTS_SCRIPT], {
    cwd: powerpacksRepoRoot,
    env: setupProcessEnv(),
    encoding: "utf8",
    input: JSON.stringify({ db: MSGVAULT_DB_PATH }),
    timeout: 20000,
  });
  let value: Record<string, number> | null = null;
  try {
    const parsed = JSON.parse(result.stdout || "null");
    if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) value = parsed;
  } catch {
    value = null;
  }
  if (value == null) return null;
  interactionCountsCache = { key, expiresAt: nowMs + INTERACTION_CACHE_TTL_MS, value };
  return value;
}

function rowInteractionCount(row: Record<string, any>, counts: Record<string, number>): number {
  const emails = new Set<string>();
  for (const email of Array.isArray(row.all_emails) ? row.all_emails : []) {
    const text = String(email || "").trim().toLowerCase();
    if (text) emails.add(text);
  }
  const primary = String(row.primary_email || "").trim().toLowerCase();
  if (primary) emails.add(primary);
  let total = 0;
  for (const email of emails) total += counts[email] || 0;
  return total;
}

// Safe sort expression mapping (prevents SQL injection via dynamic sort).
// Mirrors prod _CONTACT_SORT_MAP semantics with local column names.
const CONTACT_SORT_MAP: Record<string, string> = {
  first_name: "lower(coalesce(first_name, '') || ' ' || coalesce(last_name, ''))",
  last_name: "lower(coalesce(last_name, ''))",
  headline: "lower(coalesce(headline, ''))",
  current_company: "lower(coalesce(current_company, ''))",
  city: "lower(coalesce(city, ''))",
};

const ROW_COLUMNS = [
  "person_id",
  "first_name",
  "last_name",
  "full_name",
  "headline",
  "current_title",
  "current_company",
  "city",
  "state",
  "country",
  "location_raw",
  "primary_email",
  "all_emails",
  "primary_phone",
  "all_phones",
  "source_channels",
  "x_twitter_handle",
  "twitter_handle",
  "public_identifier",
  "public_profile_url",
  "linkedin_url",
  "profile_picture_url",
].join(", ");

function clampInt(raw: string | null, fallback: number, min: number, max: number): number {
  const parsed = parseInt(raw || "", 10);
  if (!Number.isFinite(parsed)) return fallback;
  return Math.min(max, Math.max(min, parsed));
}

export async function handleContactsRoutes(req: any, res: any, url: URL): Promise<boolean> {
  if (url.pathname === "/local-api/contacts" && req.method === "GET") {
    const q = (url.searchParams.get("q") || "").trim();
    const interactionCounts = msgvaultInteractionCounts();
    const interactionsAvailable = interactionCounts != null;
    // Default sort mirrors prod ContactsV2: total interactions desc when the
    // msgvault metadata store is available, else first_name asc.
    const defaultSort = interactionsAvailable ? "total_interactions" : "first_name";
    const sortParam = url.searchParams.get("sort") || defaultSort;
    const sortByInteractions = sortParam === "total_interactions" && interactionsAvailable;
    const sort = Object.prototype.hasOwnProperty.call(CONTACT_SORT_MAP, sortParam) ? sortParam : "first_name";
    const defaultDir = sortByInteractions ? "desc" : "asc";
    const dir = (url.searchParams.get("dir") || defaultDir).toLowerCase() === "desc" ? "DESC" : "ASC";
    const page = clampInt(url.searchParams.get("page"), 0, 0, 1_000_000);
    const pageSize = clampInt(url.searchParams.get("page_size"), 50, 1, 200);

    const duckdbPath = path.join(powerpacksRepoRoot, ".powerpacks", "search-index", "local-search.duckdb");
    if (!fs.existsSync(duckdbPath)) {
      sendJson(res, { rows: [], total: 0, page, page_size: pageSize, index_missing: true });
      return true;
    }

    // Mirrors prod: searches shorter than 2 chars are ignored.
    const parsed = q.length >= 2 ? parseContactSearch(q) : [];
    const { clause, params } = buildSearchClause(parsed);

    const countRows = queryLocalDuckdb(
      powerpacksRepoRoot,
      `SELECT count(*) AS total FROM local_person_profiles ${clause}`,
      params
    );
    const total = Number(countRows[0]?.total || 0);

    let rows: Record<string, any>[];
    if (sortByInteractions) {
      // Interaction counts live outside DuckDB (msgvault), so fetch all
      // matching rows, attach counts, sort and paginate in process. The
      // profiles table is small (hundreds of rows), so this stays cheap.
      const allRows = queryLocalDuckdb(
        powerpacksRepoRoot,
        `SELECT ${ROW_COLUMNS} FROM local_person_profiles ${clause}`,
        params
      );
      for (const row of allRows) row.total_interactions = rowInteractionCount(row, interactionCounts);
      const flip = dir === "DESC" ? -1 : 1;
      allRows.sort((a, b) => {
        if (a.total_interactions !== b.total_interactions) {
          return (a.total_interactions - b.total_interactions) * flip;
        }
        const nameA = String(a.full_name || "").toLowerCase();
        const nameB = String(b.full_name || "").toLowerCase();
        if (nameA !== nameB) return nameA < nameB ? -1 : 1;
        return String(a.person_id) < String(b.person_id) ? -1 : 1;
      });
      rows = allRows.slice(page * pageSize, (page + 1) * pageSize);
    } else {
      // Secondary keys keep pagination deterministic when the sort key ties.
      const orderBy = `${CONTACT_SORT_MAP[sort]} ${dir}, lower(coalesce(full_name, '')) ASC, person_id ASC`;
      rows = queryLocalDuckdb(
        powerpacksRepoRoot,
        `SELECT ${ROW_COLUMNS} FROM local_person_profiles ${clause} ORDER BY ${orderBy} LIMIT ? OFFSET ?`,
        [...params, pageSize, page * pageSize]
      );
      if (interactionsAvailable) {
        for (const row of rows) row.total_interactions = rowInteractionCount(row, interactionCounts);
      }
    }

    sendJson(res, { rows, total, page, page_size: pageSize, interactions_available: interactionsAvailable });
    return true;
  }

  return false;
}
