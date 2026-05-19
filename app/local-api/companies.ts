import fs from "fs";
import path from "path";
import { createGunzip } from "zlib";
import { createInterface } from "readline";
import { closeDuckDb, hasTable, openConfiguredDuckDb, queryRows, tableColumns, type DuckDbHandle } from "./duckdb";
import { toJsonSafe } from "./jsonSafe";

type Company = Record<string, any>;
type CompanyPerson = Record<string, any>;

const COMPANY_TABLES = ["local_companies", "companies"];
const PEOPLE_POSITION_TABLES = ["local_people_positions", "people_positions", "positions"];
const MAX_LIMIT = 200;

function asText(value: any): string | null {
  if (value == null) return null;
  const text = String(value).trim();
  return text && !["null", "none", "nan"].includes(text.toLowerCase()) ? text : null;
}

function asNumber(value: any): number | null {
  if (value == null || value === "") return null;
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}

function asBool(value: any): boolean {
  if (typeof value === "boolean") return value;
  return ["1", "true", "t", "yes", "y"].includes(String(value ?? "").toLowerCase());
}

function asList(value: any): any[] {
  if (value == null || value === "") return [];
  if (Array.isArray(value)) return value;
  if (typeof value === "string") {
    const text = value.trim();
    if (!text) return [];
    if (text.startsWith("[")) {
      try {
        const parsed = JSON.parse(text);
        return Array.isArray(parsed) ? parsed : [parsed];
      } catch {}
    }
    return text.split(",").map((part) => part.trim()).filter(Boolean);
  }
  return [value];
}

function first(row: any, keys: string[]): any {
  for (const key of keys) if (row?.[key] != null && row[key] !== "") return row[key];
  return null;
}

function normalizeCompany(row: any): Company {
  const name = asText(first(row, ["name", "company_name", "display_name", "organization"]));
  const id = asText(first(row, ["id", "company_id", "canonical_company_id", "company_key", "canonical_key"])) || name || "unknown";
  return {
    id,
    name: name || id,
    description: asText(first(row, ["description", "summary", "semantic_text"])),
    sector_types: asList(first(row, ["sector_types", "sectors", "entity_sector_text", "industry"])).map(String),
    entity_types: asList(first(row, ["entity_types", "entity_type"])).map(String),
    stage: asText(first(row, ["stage"])),
    headcount: asNumber(first(row, ["headcount", "employee_count", "employees"])),
    funding_total: asNumber(first(row, ["funding_total", "total_funding"])),
    city: asText(first(row, ["city"])),
    state: asText(first(row, ["state"])),
    country: asText(first(row, ["country"])),
    logo_url: asText(first(row, ["logo_url", "company_logo_url"])),
    linkedin_url: asText(first(row, ["linkedin_url", "company_linkedin_url"])),
    people_count: asNumber(first(row, ["people_count", "person_count", "count"])) || 0,
  };
}

function normalizePerson(row: any): CompanyPerson {
  const name = asText(first(row, ["name", "full_name", "display_name", "person_name"])) || asText(first(row, ["public_identifier", "person_id", "base_id"])) || "Unknown";
  const id = asText(first(row, ["id", "person_id", "base_id", "public_identifier"])) || name;
  return {
    id,
    name,
    public_identifier: asText(first(row, ["public_identifier", "linkedin_slug"])),
    position_title: asText(first(row, ["position_title", "title"])),
    position_description: asText(first(row, ["position_description", "description"])),
    seniority_band: asText(first(row, ["seniority_band"])),
    headline: asText(first(row, ["headline", "summary"])),
    is_current: asBool(first(row, ["is_current", "current"])),
    start_date: asText(first(row, ["start_date", "starts_at"])),
    end_date: asText(first(row, ["end_date", "ends_at"])),
    tenure_years: asNumber(first(row, ["tenure_years", "total_years_experience"])),
    positions_count: asNumber(first(row, ["positions_count"])) || 1,
    all_positions: asList(first(row, ["all_positions"])),
  };
}

function parseLimitOffset(url: URL) {
  const limit = Math.min(MAX_LIMIT, Math.max(1, Number(url.searchParams.get("limit") || 50) || 50));
  const offset = Math.max(0, Number(url.searchParams.get("offset") || 0) || 0);
  return { limit, offset };
}

function peopleSearch(url: URL): string | null {
  return asText(url.searchParams.get("people_search"));
}

function matchesPeopleSearch(person: CompanyPerson, search: string | null) {
  if (!search) return true;
  const q = search.toLowerCase();
  return [person.name, person.position_title, person.position_description, person.headline]
    .join(" ")
    .toLowerCase()
    .includes(q);
}

function multiParam(url: URL, key: string): string[] {
  return url.searchParams.getAll(key).flatMap((v) => v.split(",")).map((v) => v.trim().toLowerCase()).filter(Boolean);
}

function matchesFilters(company: Company, name: string | null, sectors: string[], entities: string[]) {
  if (name && !`${company.name || ""} ${company.description || ""}`.toLowerCase().includes(name.toLowerCase())) return false;
  const sectorText = asList(company.sector_types).join(" ").toLowerCase();
  const entityText = asList(company.entity_types).join(" ").toLowerCase();
  if (sectors.length && !sectors.some((s) => sectorText.includes(s))) return false;
  if (entities.length && !entities.some((e) => entityText.includes(e))) return false;
  return true;
}

async function readJsonl(filePath: string, gzipped = false): Promise<any[]> {
  if (!fs.existsSync(filePath)) return [];
  const rows: any[] = [];
  const input = gzipped ? fs.createReadStream(filePath).pipe(createGunzip()) : fs.createReadStream(filePath, { encoding: "utf8" });
  const rl = createInterface({ input, crlfDelay: Infinity });
  for await (const line of rl) {
    if (!line.trim()) continue;
    try { rows.push(JSON.parse(line)); } catch {}
  }
  return rows;
}

function parseCsvLine(line: string): string[] {
  const values: string[] = [];
  let cur = "", q = false;
  for (let i = 0; i < line.length; i++) {
    const ch = line[i];
    if (ch === '"') {
      if (q && line[i + 1] === '"') { cur += '"'; i++; } else q = !q;
    } else if (ch === "," && !q) { values.push(cur); cur = ""; }
    else cur += ch;
  }
  values.push(cur);
  return values;
}

async function readCsv(filePath: string): Promise<any[]> {
  if (!fs.existsSync(filePath)) return [];
  const rows: any[] = [];
  const rl = createInterface({ input: fs.createReadStream(filePath, { encoding: "utf8" }), crlfDelay: Infinity });
  let headers: string[] | null = null;
  for await (const line of rl) {
    if (!headers) { headers = parseCsvLine(line); continue; }
    if (!line.trim()) continue;
    const values = parseCsvLine(line);
    rows.push(Object.fromEntries(headers.map((h, i) => [h, values[i] ?? ""])));
  }
  return rows;
}

async function artifactDirs(repoRoot: string): Promise<string[]> {
  const stateRoot = path.join(repoRoot, ".powerpacks");
  const dirs = [path.join(stateRoot, "search-index")];
  const runsDir = path.join(stateRoot, "runs");
  for (const file of await fs.promises.readdir(runsDir).catch(() => [])) {
    if (!file.endsWith(".json")) continue;
    try {
      const state = JSON.parse(await fs.promises.readFile(path.join(runsDir, file), "utf8"));
      const dir = state?.artifacts?.artifact_dir;
      if (dir) dirs.push(path.resolve(repoRoot, dir));
    } catch {}
  }
  return [...new Set(dirs)];
}

function companyFromExperience(exp: any): Company | null {
  const name = asText(first(exp, ["company_name", "company", "organization", "name"]));
  const id = asText(first(exp, ["company_id", "company_urn", "company_key", "id"])) || name;
  if (!id || !name) return null;
  return normalizeCompany({ ...exp, id, name, people_count: 1 });
}

function personCompanyEntries(person: any): { company: Company; person: CompanyPerson }[] {
  const name = asText(first(person, ["name", "full_name", "display_name"]));
  const personId = asText(first(person, ["id", "person_id", "base_id", "public_identifier"]));
  const exps = asList(first(person, ["work_experiences", "experiences", "positions"]));
  const out: { company: Company; person: CompanyPerson }[] = [];
  for (const exp of exps) {
    if (!exp || typeof exp !== "object") continue;
    const company = companyFromExperience(exp);
    if (!company) continue;
    out.push({ company, person: normalizePerson({ ...person, ...exp, id: personId, name, position_title: first(exp, ["position_title", "title"]), is_current: first(exp, ["is_current", "current"]) }) });
  }
  return out;
}

async function loadFileCompanies(repoRoot: string): Promise<{ companies: Company[]; peopleByCompany: Map<string, CompanyPerson[]>; source: string; warnings: string[] }> {
  const warnings: string[] = [];
  const peopleByCompany = new Map<string, CompanyPerson[]>();
  const byId = new Map<string, Company>();
  const dirs = await artifactDirs(repoRoot);
  for (const dir of dirs) {
    const corpus = path.join(dir, "company", "companies_corpus.jsonl");
    for (const row of await readJsonl(corpus)) {
      const company = normalizeCompany(row);
      byId.set(company.id, { ...(byId.get(company.id) || {}), ...company });
    }
  }
  for (const dir of dirs) {
    for (const [file, gz] of [[path.join(dir, "hydrate_people", "llm_profiles.jsonl"), false] as const, [path.join(dir, "hydrate_people", "profiles.jsonl.gz"), true] as const]) {
      for (const row of await readJsonl(file, gz)) {
        for (const entry of personCompanyEntries(row)) {
          byId.set(entry.company.id, { ...(byId.get(entry.company.id) || {}), ...entry.company });
          const arr = peopleByCompany.get(entry.company.id) || [];
          arr.push(entry.person);
          peopleByCompany.set(entry.company.id, arr);
        }
      }
    }
  }
  const peopleCsv = path.join(repoRoot, ".powerpacks", "network-import", "merged", "people.csv");
  for (const row of await readCsv(peopleCsv)) {
    for (const entry of personCompanyEntries(row)) {
      byId.set(entry.company.id, { ...(byId.get(entry.company.id) || {}), ...entry.company });
      const arr = peopleByCompany.get(entry.company.id) || [];
      arr.push(entry.person);
      peopleByCompany.set(entry.company.id, arr);
    }
  }
  for (const [id, people] of peopleByCompany.entries()) {
    const company = byId.get(id);
    if (company) company.people_count = Math.max(company.people_count || 0, new Set(people.map((p) => p.id)).size);
  }
  if (!byId.size) warnings.push("No company data found in POWERPACKS_LOCAL_SEARCH_DB, .powerpacks/search-index, run artifact dirs, or merged people.csv");
  return { companies: [...byId.values()].sort((a, b) => String(a.name).localeCompare(String(b.name))), peopleByCompany, source: byId.size ? "files" : "empty", warnings };
}

async function chooseTable(handle: DuckDbHandle, tables: string[]): Promise<string | null> {
  for (const table of tables) if (await hasTable(handle, table)) return table;
  return null;
}

function col(cols: string[], choices: string[]): string | null {
  return choices.find((c) => cols.includes(c)) || null;
}

async function queryDuckCompanies(handle: DuckDbHandle, url: URL, table: string) {
  const { limit, offset } = parseLimitOffset(url);
  const cols = await tableColumns(handle, table);
  const nameCol = col(cols, ["name", "company_name", "display_name"]);
  const sectorCol = col(cols, ["sector_types", "sectors", "entity_sector_text", "industry"]);
  const entityCol = col(cols, ["entity_types", "entity_type"]);
  const where: string[] = [];
  const params: any[] = [];
  const name = asText(url.searchParams.get("name"));
  if (name && nameCol) { where.push(`lower(cast(${nameCol} as varchar)) like ?`); params.push(`%${name.toLowerCase()}%`); }
  for (const value of multiParam(url, "sector_types")) if (sectorCol) { where.push(`lower(cast(${sectorCol} as varchar)) like ?`); params.push(`%${value}%`); }
  for (const value of multiParam(url, "entity_types")) if (entityCol) { where.push(`lower(cast(${entityCol} as varchar)) like ?`); params.push(`%${value}%`); }
  const whereSql = where.length ? ` where ${where.join(" and ")}` : "";
  const countRows = await queryRows(handle, `select count(*) as total from ${table}${whereSql}`, params);
  const order = nameCol ? ` order by lower(cast(${nameCol} as varchar)) asc` : "";
  const rows = await queryRows(handle, `select * from ${table}${whereSql}${order} limit ? offset ?`, [...params, limit, offset]);
  return { companies: rows.map(normalizeCompany), total: Number(countRows[0]?.total || 0), limit, offset, source: `duckdb:${table}`, warnings: [] };
}

async function duckCompanyById(handle: DuckDbHandle, table: string, id: string): Promise<Company | null> {
  const cols = await tableColumns(handle, table);
  const idCol = col(cols, ["id", "company_id", "canonical_company_id", "company_key", "canonical_key"]);
  if (!idCol) return null;
  const rows = await queryRows(handle, `select * from ${table} where ${idCol} = ? limit 1`, [id]);
  return rows[0] ? normalizeCompany(rows[0]) : null;
}

async function duckPeopleForCompany(handle: DuckDbHandle, company: Company, url: URL): Promise<{ people: CompanyPerson[]; total: number }> {
  const table = await chooseTable(handle, PEOPLE_POSITION_TABLES);
  if (!table) return { people: [], total: 0 };
  const cols = await tableColumns(handle, table);
  const cid = col(cols, ["company_id", "canonical_company_id"]);
  const cname = col(cols, ["company_name", "name"]);
  if (!cid && !cname) return { people: [], total: 0 };
  const { limit, offset } = { limit: Math.min(MAX_LIMIT, Math.max(1, Number(url.searchParams.get("people_limit") || 25) || 25)), offset: Math.max(0, Number(url.searchParams.get("people_offset") || 0) || 0) };
  const where = [cid ? `${cid} = ?` : `lower(cast(${cname} as varchar)) = ?`];
  const params: any[] = [cid ? company.id : String(company.name).toLowerCase()];
  const search = peopleSearch(url);
  if (search) {
    const searchCols = [col(cols, ["name", "full_name", "display_name", "person_name"]), col(cols, ["position_title", "title"]), col(cols, ["position_description", "description"]), col(cols, ["headline", "summary"])].filter(Boolean) as string[];
    if (searchCols.length) {
      where.push(`(${searchCols.map((c) => `lower(cast(${c} as varchar)) like ?`).join(" or ")})`);
      params.push(...searchCols.map(() => `%${search.toLowerCase()}%`));
    }
  }
  const sort = url.searchParams.get("people_sort") || "current";
  const dir = (url.searchParams.get("people_dir") || "desc").toLowerCase() === "asc" ? "asc" : "desc";
  let order = "";
  if (sort === "name" && col(cols, ["name", "full_name", "display_name"])) order = ` order by ${col(cols, ["name", "full_name", "display_name"])} ${dir}`;
  else if (sort === "tenure" && col(cols, ["tenure_years", "total_years_experience"])) order = ` order by ${col(cols, ["tenure_years", "total_years_experience"])} ${dir}`;
  else if (col(cols, ["is_current", "current"])) order = ` order by ${col(cols, ["is_current", "current"])} ${dir}`;
  const whereSql = where.join(" and ");
  const countRows = await queryRows(handle, `select count(*) as total from ${table} where ${whereSql}`, params);
  const rows = await queryRows(handle, `select * from ${table} where ${whereSql}${order} limit ? offset ?`, [...params, limit, offset]);
  return { people: rows.map(normalizePerson), total: Number(countRows[0]?.total || 0) };
}

function sortPeople(people: CompanyPerson[], sort: string, dir: string) {
  const sign = dir === "asc" ? 1 : -1;
  return [...people].sort((a, b) => {
    if (sort === "name") return sign * String(a.name || "").localeCompare(String(b.name || ""));
    if (sort === "tenure") return sign * ((a.tenure_years || 0) - (b.tenure_years || 0));
    return sign * (Number(a.is_current) - Number(b.is_current));
  });
}

export async function getCompanies(url: URL, repoRoot: string) {
  const handle = await openConfiguredDuckDb();
  try {
    if (handle) {
      const table = await chooseTable(handle, COMPANY_TABLES);
      if (table) return toJsonSafe(await queryDuckCompanies(handle, url, table));
    }
  } finally { closeDuckDb(handle); }
  const { limit, offset } = parseLimitOffset(url);
  const files = await loadFileCompanies(repoRoot);
  const filtered = files.companies.filter((c) => matchesFilters(c, asText(url.searchParams.get("name")), multiParam(url, "sector_types"), multiParam(url, "entity_types")));
  return toJsonSafe({ companies: filtered.slice(offset, offset + limit), total: filtered.length, limit, offset, source: files.source, warnings: files.warnings });
}

export async function getCompanyDetail(url: URL, repoRoot: string, id: string) {
  const includePeople = ["1", "true", "yes"].includes(String(url.searchParams.get("include_people") || "").toLowerCase());
  const peopleLimit = Math.min(MAX_LIMIT, Math.max(1, Number(url.searchParams.get("people_limit") || 25) || 25));
  const peopleOffset = Math.max(0, Number(url.searchParams.get("people_offset") || 0) || 0);
  const handle = await openConfiguredDuckDb();
  try {
    if (handle) {
      const table = await chooseTable(handle, COMPANY_TABLES);
      if (table) {
        const company = await duckCompanyById(handle, table, id);
        if (company) {
          if (includePeople) {
            const result = await duckPeopleForCompany(handle, company, url);
            company.people = result.people;
            company.people_offset = peopleOffset;
            company.people_limit = peopleLimit;
            company.people_has_more = peopleOffset + result.people.length < result.total;
            company.people_count = Math.max(company.people_count || 0, result.total);
          }
          return toJsonSafe({ company, source: `duckdb:${table}`, warnings: [] });
        }
      }
    }
  } finally { closeDuckDb(handle); }
  const files = await loadFileCompanies(repoRoot);
  const company = files.companies.find((c) => c.id === id || encodeURIComponent(c.id) === id);
  if (!company) return toJsonSafe({ error: "Company not found", source: files.source, warnings: files.warnings });
  if (includePeople) {
    const people = sortPeople((files.peopleByCompany.get(company.id) || []).filter((person) => matchesPeopleSearch(person, peopleSearch(url))), url.searchParams.get("people_sort") || "current", (url.searchParams.get("people_dir") || "desc").toLowerCase());
    company.people = people.slice(peopleOffset, peopleOffset + peopleLimit);
    company.people_offset = peopleOffset;
    company.people_limit = peopleLimit;
    company.people_has_more = peopleOffset + company.people.length < people.length;
    company.people_count = Math.max(company.people_count || 0, new Set(people.map((p) => p.id)).size);
  }
  return toJsonSafe({ company, source: files.source, warnings: files.warnings });
}

export async function getCompanyAutocomplete(url: URL, repoRoot: string) {
  const limit = Math.min(50, Math.max(1, Number(url.searchParams.get("limit") || 10) || 10));
  const q = asText(url.searchParams.get("q")) || "";
  const copy = new URL(url.toString());
  copy.searchParams.set("name", q);
  copy.searchParams.set("limit", String(limit));
  copy.searchParams.set("offset", "0");
  const response: any = await getCompanies(copy, repoRoot);
  return { data: (response.companies || []).map((c: Company) => ({ id: c.id, name: c.name })), source: response.source, warnings: response.warnings || [] };
}

