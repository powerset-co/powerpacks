import fs from "fs";
import fsp from "fs/promises";
import path from "path";
import { createGunzip } from "zlib";
import { createInterface } from "readline";
import { closeDuckDb, hasTable, openConfiguredDuckDb, queryRows, tableColumns } from "./duckdb";
import { sendJson, toJsonSafe } from "./jsonSafe";

const MAX_PAGE_SIZE = 200;
const SAFE_COL = /^[A-Za-z_][A-Za-z0-9_]*$/;
const SORT_ALIASES: Record<string, string> = {
  first_name: "first_name",
  last_name: "last_name",
  headline: "headline",
  location_raw: "location_raw",
  total_interactions: "total_messages",
  total_messages: "total_messages",
};

type Query = { page: number; pageSize: number; search: string; sortField: string; sortDir: "asc" | "desc" };
type Contact = Record<string, any>;

function parseQuery(url: URL): Query | { error: string } {
  const page = Number(url.searchParams.get("page") || 0);
  const pageSize = Number(url.searchParams.get("page_size") || 50);
  if (!Number.isInteger(page) || page < 0) return { error: "page must be an integer >= 0" };
  if (!Number.isInteger(pageSize) || pageSize < 1 || pageSize > MAX_PAGE_SIZE) return { error: "page_size must be an integer between 1 and 200" };
  const requestedSort = url.searchParams.get("sort_field") || "last_name";
  if (!(requestedSort in SORT_ALIASES)) return { error: "unsupported sort_field" };
  const sortDir = (url.searchParams.get("sort_dir") || "asc").toLowerCase();
  if (sortDir !== "asc" && sortDir !== "desc") return { error: "sort_dir must be asc or desc" };
  return { page, pageSize, search: (url.searchParams.get("search") || "").trim(), sortField: requestedSort, sortDir };
}

function parseJsonish(value: any): any {
  if (value == null || Array.isArray(value) || typeof value === "object") return value;
  const text = String(value).trim();
  if (!text) return null;
  if (text.startsWith("[") || text.startsWith("{")) {
    try { return JSON.parse(text); } catch { return value; }
  }
  return value;
}
function listValue(value: any): any[] {
  const parsed = parseJsonish(value);
  if (parsed == null || parsed === "") return [];
  if (Array.isArray(parsed)) return parsed.map(String).filter(Boolean);
  return String(parsed).split(/[;,]/).map((s) => s.trim()).filter(Boolean);
}
function emailList(value: any): string[] { return Array.from(new Set(listValue(value).map((s) => s.toLowerCase()))); }
function str(value: any): string | null { const s = value == null ? "" : String(value).trim(); return s || null; }
function num(value: any): number { const n = Number(value); return Number.isFinite(n) ? Math.trunc(n) : 0; }

function normalizeContact(row: Contact): Contact {
  const emails = emailList(row.all_emails);
  const primary = str(row.primary_email)?.toLowerCase() || emails[0] || null;
  if (primary && !emails.includes(primary)) emails.unshift(primary);
  const totalMessages = num(row.total_messages ?? row.total_interactions);
  return toJsonSafe({
    id: str(row.id) || primary || str(row.display_name) || "unknown",
    operator_id: str(row.operator_id),
    gmail_token_id: str(row.gmail_token_id),
    primary_email: primary,
    display_name: str(row.display_name) || [str(row.first_name), str(row.last_name)].filter(Boolean).join(" ") || primary,
    first_name: str(row.first_name),
    last_name: str(row.last_name),
    all_emails: emails,
    domain: str(row.domain) || (primary?.includes("@") ? primary.split("@")[1] : null),
    headline: str(row.headline),
    location_raw: str(row.location_raw ?? row.location),
    linkedin_url: str(row.linkedin_url ?? row.confirmed_linkedin_url ?? row.llm_selected_linkedin ?? row.pass1_linkedin_url),
    x_url: str(row.x_url ?? row.twitter_url),
    phone_numbers: listValue(row.phone_numbers ?? row.phone ?? row.mobile_phone),
    total_messages: totalMessages,
    total_interactions: num(row.total_interactions ?? totalMessages),
    pass1_status: str(row.pass1_status),
    pass1_linkedin_url: str(row.pass1_linkedin_url),
    potential_linkedins: parseJsonish(row.potential_linkedins) ?? [],
    candidate_count: num(row.candidate_count),
    sources_searched: parseJsonish(row.sources_searched) ?? [],
    llm_selected_linkedin: str(row.llm_selected_linkedin),
    llm_confidence: row.llm_confidence == null || row.llm_confidence === "" ? null : Number(row.llm_confidence),
    llm_reasoning: str(row.llm_reasoning),
    confirmed_linkedin_url: str(row.confirmed_linkedin_url),
    confirmation_source: str(row.confirmation_source),
    verification_notes: str(row.verification_notes),
    created_at: row.created_at ?? null,
    updated_at: row.updated_at ?? null,
  }) as Contact;
}

function parseCsvLine(line: string): string[] {
  const out: string[] = []; let cur = ""; let q = false;
  for (let i = 0; i < line.length; i++) { const c = line[i]; if (c === '"') { if (q && line[i+1] === '"') { cur += '"'; i++; } else q = !q; } else if (c === "," && !q) { out.push(cur); cur = ""; } else cur += c; }
  out.push(cur); return out;
}
async function readCsv(file: string): Promise<Contact[]> {
  if (!fs.existsSync(file)) return [];
  const lines = (await fsp.readFile(file, "utf8")).split(/\r?\n/).filter((l) => l.trim());
  if (!lines.length) return [];
  const headers = parseCsvLine(lines[0]);
  return lines.slice(1).map((line) => Object.fromEntries(headers.map((h, i) => [h, parseCsvLine(line)[i] ?? ""])));
}
async function readJsonl(file: string, gz = false): Promise<Contact[]> {
  if (!fs.existsSync(file)) return [];
  const input = gz ? fs.createReadStream(file).pipe(createGunzip()) : fs.createReadStream(file, { encoding: "utf8" });
  const rl = createInterface({ input, crlfDelay: Infinity });
  const rows: Contact[] = [];
  for await (const line of rl) if (line.trim()) { try { rows.push(JSON.parse(line)); } catch {} }
  return rows;
}
async function discoverProfileArtifacts(root: string): Promise<string[]> {
  const runs = path.join(root, ".powerpacks", "runs"); const out: string[] = [];
  for (const file of await fsp.readdir(runs).catch(() => [])) {
    if (!file.endsWith(".json")) continue;
    try {
      const state = JSON.parse(await fsp.readFile(path.join(runs, file), "utf8"));
      const dir = state?.artifacts?.artifact_dir ? path.resolve(root, state.artifacts.artifact_dir) : null;
      if (dir?.startsWith(root)) {
        for (const rel of ["hydrate_people/llm_profiles.jsonl", "hydrate_people/profiles.jsonl.gz"]) {
          const p = path.join(dir, rel); if (fs.existsSync(p)) out.push(p);
        }
      }
    } catch {}
  }
  return out;
}
function profileToContact(row: Contact): Contact {
  return normalizeContact({
    id: row.person_id ?? row.id ?? row.public_identifier,
    display_name: row.name ?? row.full_name ?? row.display_name,
    first_name: row.first_name,
    last_name: row.last_name,
    headline: row.headline ?? row.title,
    location_raw: row.location_raw ?? row.location,
    linkedin_url: row.linkedin_url ?? row.url,
    x_url: row.x_url ?? row.twitter_url,
    primary_email: row.primary_email ?? row.email,
    all_emails: row.all_emails ?? row.emails,
    phone_numbers: row.phone_numbers ?? row.phones,
    total_messages: row.total_messages ?? row.total_interactions,
  });
}
async function loadArtifactContacts(root: string): Promise<{ rows: Contact[]; source: string; warnings: string[] }> {
  const pp = path.join(root, ".powerpacks"); const warnings: string[] = []; const rows: Contact[] = [];
  const candidates = [path.join(pp, "network-import/merged/people.csv"), path.join(pp, "messages/contacts.csv")];
  const msgDir = path.join(pp, "messages");
  for (const f of await fsp.readdir(msgDir).catch(() => [])) if (f.endsWith("contacts.csv")) candidates.push(path.join(msgDir, f));
  for (const file of Array.from(new Set(candidates))) {
    const got = await readCsv(file); if (got.length) rows.push(...got.map(normalizeContact)); else warnings.push(`checked ${path.relative(root, file)}`);
  }
  for (const file of await discoverProfileArtifacts(root)) rows.push(...(await readJsonl(file, file.endsWith(".gz"))).map(profileToContact));
  const byId = new Map<string, Contact>(); for (const r of rows) byId.set(String(r.id), { ...(byId.get(String(r.id)) || {}), ...r });
  return { rows: [...byId.values()], source: rows.length ? "artifacts" : "none", warnings };
}

function matches(row: Contact, search: string): boolean {
  if (!search) return true; const q = search.toLowerCase();
  const field = q.match(/^(headline|email|phone):(.*)$/); const term = (field ? field[2] : q).trim();
  if (!term) return true;
  if (field?.[1] === "headline") return String(row.headline || "").toLowerCase().includes(term);
  if (field?.[1] === "email") return [row.primary_email, ...(row.all_emails || [])].join(" ").toLowerCase().includes(term);
  if (field?.[1] === "phone") return (row.phone_numbers || []).join(" ").toLowerCase().includes(term);
  return [row.display_name, row.first_name, row.last_name].join(" ").toLowerCase().includes(term);
}
function pageRows(rows: Contact[], q: Query) {
  const filtered = rows.filter((r) => matches(r, q.search));
  const key = q.sortField === "total_interactions" ? "total_messages" : q.sortField;
  filtered.sort((a, b) => { const av = a[key] ?? ""; const bv = b[key] ?? ""; const cmp = typeof av === "number" || typeof bv === "number" ? Number(av) - Number(bv) : String(av).localeCompare(String(bv)); return q.sortDir === "desc" ? -cmp : cmp; });
  return { data: filtered.slice(q.page * q.pageSize, q.page * q.pageSize + q.pageSize), total_count: filtered.length };
}

async function queryDuck(table: "local_contacts" | "linkedin_candidates", q: Query) {
  const handle = await openConfiguredDuckDb(); if (!handle) return null;
  try {
    if (!(await hasTable(handle, table))) return null;
    const cols = await tableColumns(handle, table); const colSet = new Set(cols);
    const sort = SORT_ALIASES[q.sortField]; const sortCol = colSet.has(sort) && SAFE_COL.test(sort) ? sort : (colSet.has("last_name") ? "last_name" : "id");
    const where: string[] = []; const params: unknown[] = [];
    const addLike = (col: string) => { if (colSet.has(col) && SAFE_COL.test(col)) { where.push(`coalesce(cast(${col} as varchar), '') ilike ?`); params.push(`%${term}%`); } };
    let term = q.search; const p = q.search.toLowerCase().match(/^(headline|email|phone):(.*)$/); if (p) term = p[2].trim();
    if (term) {
      const before = where.length;
      if (p?.[1] === "headline") addLike("headline");
      else if (p?.[1] === "email") { addLike("primary_email"); addLike("all_emails"); }
      else if (p?.[1] === "phone") addLike("phone_numbers");
      else { addLike("display_name"); addLike("first_name"); addLike("last_name"); }
      if (where.length > before) where.splice(before, where.length - before, `(${where.slice(before).join(" or ")})`);
    }
    const whereSql = where.length ? `where ${where.join(" and ")}` : "";
    const totalRows = await queryRows(handle, `select count(*) as count from ${table} ${whereSql}`, params);
    const rows = await queryRows(handle, `select * from ${table} ${whereSql} order by ${sortCol} ${q.sortDir === "desc" ? "desc" : "asc"} limit ? offset ?`, [...params, q.pageSize, q.page * q.pageSize]);
    return { data: rows.map(normalizeContact), total_count: Number(totalRows[0]?.count || 0), source: `duckdb:${table}`, warnings: [] };
  } finally { closeDuckDb(handle); }
}

export async function handleContactsRequest(req: any, res: any, next: any, opts: { repoRoot: string }) {
  const url = new URL(req.url || "/", "http://localhost");
  if (url.pathname !== "/local-api/contacts") return next();
  const q = parseQuery(url); if ("error" in q) return sendJson(res, { error: q.error }, 400);
  const duck = await queryDuck("local_contacts", q) || await queryDuck("linkedin_candidates", q);
  if (duck) return sendJson(res, duck);
  const loaded = await loadArtifactContacts(opts.repoRoot); const paged = pageRows(loaded.rows, q);
  const warnings = loaded.rows.length ? loaded.warnings : [...loaded.warnings, "no contacts source found"];
  return sendJson(res, { ...paged, source: loaded.source, warnings });
}
