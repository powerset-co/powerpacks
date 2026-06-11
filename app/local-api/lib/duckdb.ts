import path from "path";
import fs from "fs";
import { spawnSync } from "child_process";

import { powerpacksRepoRoot } from "./paths";
import { setupProcessEnv } from "./env";

const DUCKDB_CACHE_TTL_MS = 10000;

type DuckdbRow = Record<string, any>;
type QueryLocalDuckdbOptions = { duckdbPath?: string; timeoutMs?: number };

let cachedDuckdbTables: { key: string; expiresAt: number; value: Array<{ name: string; rows: number; vectorRows?: number; vectorPeople?: number }> } | null = null;
const duckdbQueryCache = new Map<string, { expiresAt: number; value: DuckdbRow[] }>();

export function clearLocalDuckdbTableCountsCache() {
  cachedDuckdbTables = null;
}

function pruneDuckdbQueryCache(nowMs: number) {
  for (const [key, entry] of duckdbQueryCache) {
    if (entry.expiresAt <= nowMs) duckdbQueryCache.delete(key);
  }
}

// Read-only, parameterized duckdb query. SQL and params travel to the inline
// python as a JSON payload over stdin, so values are never interpolated into
// the SQL string.
const QUERY_LOCAL_DUCKDB_SCRIPT = `
import duckdb, json, sys
payload = json.load(sys.stdin)
con = duckdb.connect(payload["db"], read_only=True)
cursor = con.execute(payload["sql"], payload.get("params") or [])
columns = [description[0] for description in cursor.description or []]
print(json.dumps([dict(zip(columns, row)) for row in cursor.fetchall()], default=str))
`;

export function queryLocalDuckdb(repoRoot: string, sql: string, params: unknown[] = [], options: QueryLocalDuckdbOptions = {}): DuckdbRow[] {
  const duckdbPath = options.duckdbPath || path.join(repoRoot, ".powerpacks", "search-index", "local-search.duckdb");
  if (!duckdbPath || !fs.existsSync(duckdbPath)) return [];
  const stat = fs.statSync(duckdbPath);
  const key = `${duckdbPath}:${stat.mtimeMs}:${stat.size}:${sql}:${JSON.stringify(params)}`;
  const nowMs = Date.now();
  pruneDuckdbQueryCache(nowMs);
  const cached = duckdbQueryCache.get(key);
  if (cached && cached.expiresAt > nowMs) return cached.value;
  const result = spawnSync("uv", ["run", "--project", ".", "python", "-c", QUERY_LOCAL_DUCKDB_SCRIPT], {
    cwd: repoRoot,
    env: setupProcessEnv(),
    encoding: "utf8",
    input: JSON.stringify({ db: duckdbPath, sql, params }),
    timeout: options.timeoutMs ?? 10000,
  });
  let value: DuckdbRow[] = [];
  try {
    const parsed = JSON.parse(result.stdout || "[]");
    if (Array.isArray(parsed)) value = parsed;
  } catch {
    value = [];
  }
  duckdbQueryCache.set(key, { expiresAt: nowMs + DUCKDB_CACHE_TTL_MS, value });
  return value;
}

export function localDuckdbTableCounts(duckdbPath: string): Array<{ name: string; rows: number }> {
  if (!duckdbPath || !fs.existsSync(duckdbPath)) return [];
  const stat = fs.statSync(duckdbPath);
  const key = `${duckdbPath}:${stat.mtimeMs}:${stat.size}`;
  const nowMs = Date.now();
  if (cachedDuckdbTables && cachedDuckdbTables.key === key && cachedDuckdbTables.expiresAt > nowMs) {
    return cachedDuckdbTables.value;
  }
  const script = `
import duckdb, json
tables = ["local_person_profiles", "local_people_positions", "local_summaries", "local_people_education", "local_education", "local_companies"]
out = []
con = duckdb.connect(${JSON.stringify(duckdbPath)}, read_only=True)
for table in tables:
    try:
        row = {"name": table, "rows": int(con.execute(f"select count(*) from {table}").fetchone()[0])}
        out.append(row)
    except Exception:
        pass
print(json.dumps(out))
`;
  const result = spawnSync("uv", ["run", "--project", ".", "python", "-c", script], {
    cwd: powerpacksRepoRoot,
    env: setupProcessEnv(),
    encoding: "utf8",
    timeout: 10000,
  });
  let value: Array<{ name: string; rows: number }> = [];
  try {
    const parsed = JSON.parse(result.stdout || "[]");
    if (Array.isArray(parsed)) {
      value = parsed
        .map((row) => ({
          name: String(row.name || ""),
          rows: Number(row.rows || 0),
        }))
        .filter((row) => row.name);
    }
  } catch {
    value = [];
  }
  cachedDuckdbTables = { key, expiresAt: nowMs + 10000, value };
  return value;
}
