import { defineConfig, type Plugin } from "vite";
import react from "@vitejs/plugin-react-swc";
import path from "path";
import fs from "fs";
import fsp from "fs/promises";
import { createGunzip } from "zlib";
import { createInterface } from "readline";

const powerpacksRepoRoot = path.resolve(
  __dirname,
  process.env.POWERPACKS_REPO_ROOT || ".."
);
const powerpacksStateRoot = path.join(powerpacksRepoRoot, ".powerpacks");
const runsDir = path.join(powerpacksStateRoot, "runs");

type RunState = Record<string, any>;

function sendJson(res: any, data: unknown, status = 200) {
  res.statusCode = status;
  res.setHeader("Content-Type", "application/json; charset=utf-8");
  res.end(JSON.stringify(data));
}

function safeJoinPowerpacks(relativePath: string | undefined | null): string | null {
  if (!relativePath) return null;
  const resolved = path.resolve(powerpacksRepoRoot, relativePath);
  if (!resolved.startsWith(powerpacksRepoRoot)) return null;
  return resolved;
}

function readJsonSync(filePath: string): RunState | null {
  try {
    return JSON.parse(fs.readFileSync(filePath, "utf8"));
  } catch {
    return null;
  }
}

function extractConversationId(taskId: string): string {
  return taskId.replace(/^search-network-/, "").match(/^[0-9a-f-]{36}/i)?.[0] || taskId;
}

function summarizeRun(filePath: string) {
  const state = readJsonSync(filePath) || {};
  const stat = fs.statSync(filePath);
  const artifacts = state.artifacts || {};
  const taskId = state.task_id || path.basename(filePath, ".json");
  return {
    taskId,
    conversationId: extractConversationId(taskId),
    fileName: path.basename(filePath),
    query: state.query || artifacts.query || path.basename(filePath, ".json"),
    status: state.status || "unknown",
    task: state.task,
    createdAt: state.created_at || artifacts.created_at || null,
    updatedAt: state.updated_at || state.created_at || artifacts.created_at || null,
    mtimeMs: stat.mtimeMs,
    rowCount: artifacts.row_count ?? null,
    hydratedCount: artifacts.hydrated_count ?? null,
    hasArtifacts: Boolean(artifacts.jsonl || artifacts.csv),
    artifactDir: artifacts.artifact_dir || null,
  };
}

async function listRuns() {
  const files = await fsp.readdir(runsDir).catch(() => []);
  return files
    .filter((file) => file.endsWith(".json") && file.startsWith("search-network-"))
    .map((file) => path.join(runsDir, file))
    .filter((filePath) => fs.existsSync(filePath))
    .map(summarizeRun)
    .sort((a, b) => b.mtimeMs - a.mtimeMs);
}

async function findRun(taskId: string) {
  const runs = await listRuns();
  const summary = runs.find((run) => run.taskId === taskId || run.conversationId === taskId || run.fileName === taskId || run.fileName.startsWith(`${taskId}-`) || run.fileName.startsWith(`search-network-${taskId}`));
  if (!summary) return null;
  const statePath = path.join(runsDir, summary.fileName);
  const state = readJsonSync(statePath) || {};
  return { summary, state, statePath };
}

async function readJsonlWindow(filePath: string, offset: number, limit: number): Promise<any[]> {
  if (!filePath || !fs.existsSync(filePath)) return [];
  const rows: any[] = [];
  const input = fs.createReadStream(filePath, { encoding: "utf8" });
  const rl = createInterface({ input, crlfDelay: Infinity });
  let index = 0;
  for await (const line of rl) {
    if (!line.trim()) continue;
    if (index >= offset && rows.length < limit) rows.push(JSON.parse(line));
    index += 1;
    if (rows.length >= limit) {
      rl.close();
      input.destroy();
      break;
    }
  }
  return rows;
}

async function readJsonlForIds(filePath: string, ids: Set<string>): Promise<Record<string, any>> {
  const rows: Record<string, any> = {};
  if (!filePath || !fs.existsSync(filePath) || ids.size === 0) return rows;
  const input = fs.createReadStream(filePath, { encoding: "utf8" });
  const rl = createInterface({ input, crlfDelay: Infinity });
  for await (const line of rl) {
    if (!line.trim()) continue;
    const row = JSON.parse(line);
    const personId = String(row.person_id || "");
    if (ids.has(personId)) {
      rows[personId] = row;
      if (Object.keys(rows).length >= ids.size) {
        rl.close();
        input.destroy();
        break;
      }
    }
  }
  return rows;
}

function parseCsvLine(line: string): string[] {
  const values: string[] = [];
  let current = "";
  let inQuotes = false;
  for (let i = 0; i < line.length; i += 1) {
    const char = line[i];
    if (char === '"') {
      if (inQuotes && line[i + 1] === '"') {
        current += '"';
        i += 1;
      } else {
        inQuotes = !inQuotes;
      }
    } else if (char === "," && !inQuotes) {
      values.push(current);
      current = "";
    } else {
      current += char;
    }
  }
  values.push(current);
  return values;
}

async function readCsvWindow(filePath: string, offset: number, limit: number): Promise<{ rows: any[]; total: number }> {
  if (!filePath || !fs.existsSync(filePath)) return { rows: [], total: 0 };
  const rows: any[] = [];
  const input = fs.createReadStream(filePath, { encoding: "utf8" });
  const rl = createInterface({ input, crlfDelay: Infinity });
  let headers: string[] | null = null;
  let index = 0;
  for await (const line of rl) {
    if (!headers) {
      headers = parseCsvLine(line);
      continue;
    }
    if (!line.trim()) continue;
    if (index >= offset && rows.length < limit) {
      const values = parseCsvLine(line);
      rows.push(Object.fromEntries(headers.map((header, i) => [header, values[i] ?? ""])));
    }
    index += 1;
  }
  return { rows, total: index };
}

async function readProfilesForIds(filePath: string, ids: Set<string>, gzipped = false): Promise<Record<string, any>> {
  const profiles: Record<string, any> = {};
  if (!filePath || !fs.existsSync(filePath) || ids.size === 0) return profiles;

  const input = gzipped
    ? fs.createReadStream(filePath).pipe(createGunzip())
    : fs.createReadStream(filePath, { encoding: "utf8" });
  const rl = createInterface({ input, crlfDelay: Infinity });

  for await (const line of rl) {
    if (!line.trim()) continue;
    const profile = JSON.parse(line);
    const personId = String(profile.person_id || "");
    if (ids.has(personId)) {
      profiles[personId] = profile;
      if (Object.keys(profiles).length >= ids.size) {
        rl.close();
        input.destroy();
        break;
      }
    }
  }
  return profiles;
}

async function loadResults(state: RunState, offset = 0, limit = 50) {
  const artifacts = state.artifacts || {};
  const jsonlPath = safeJoinPowerpacks(artifacts.jsonl);
  const artifactDir = safeJoinPowerpacks(artifacts.artifact_dir);
  const rerankCsv = artifactDir ? path.join(artifactDir, "llm_rerank_candidates", "query_results.csv") : null;

  let rows: any[] = [];
  let totalRows = Number(artifacts.row_count ?? 0) || null;

  if (rerankCsv && fs.existsSync(rerankCsv)) {
    const { rows: rerankRows, total } = await readCsvWindow(rerankCsv, offset, limit);
    totalRows = total;
    const ids = new Set(rerankRows.map((row) => String(row.person_id || "")).filter(Boolean));
    const baseRowsById = jsonlPath ? await readJsonlForIds(jsonlPath, ids) : {};
    rows = rerankRows.map((row) => ({
      ...(baseRowsById[String(row.person_id)] || {}),
      ...row,
      rank: Number(row.result_index ?? 0) + 1,
      reranked: true,
    }));
  } else {
    rows = jsonlPath ? await readJsonlWindow(jsonlPath, offset, limit) : [];
  }

  let profiles: Record<string, any> = {};
  if (artifactDir) {
    const ids = new Set(rows.map((row) => String(row.person_id || "")).filter(Boolean));
    const llmProfiles = path.join(artifactDir, "hydrate_people", "llm_profiles.jsonl");
    const gzProfiles = path.join(artifactDir, "hydrate_people", "profiles.jsonl.gz");
    profiles = fs.existsSync(llmProfiles)
      ? await readProfilesForIds(llmProfiles, ids)
      : fs.existsSync(gzProfiles)
        ? await readProfilesForIds(gzProfiles, ids, true)
        : {};
  }

  return {
    rows,
    profiles,
    offset,
    limit,
    totalRows,
    hasMore: totalRows != null ? offset + rows.length < totalRows : rows.length >= limit,
  };
}

function powerpacksLocalApiPlugin(): Plugin {
  return {
    name: "powerpacks-local-api",
    configureServer(server) {
      server.middlewares.use(async (req, res, next) => {
        try {
          const url = new URL(req.url || "/", "http://localhost");
          if (url.pathname === "/local-api/runs") {
            return sendJson(res, await listRuns());
          }

          const match = url.pathname.match(/^\/local-api\/runs\/([^/]+)\/results$/);
          if (match) {
            const taskId = decodeURIComponent(match[1]);
            const found = await findRun(taskId);
            if (!found) return sendJson(res, { error: "Run not found" }, 404);
            const offset = Math.max(0, Number(url.searchParams.get("offset") || 0) || 0);
            const limit = Math.min(200, Math.max(1, Number(url.searchParams.get("limit") || 50) || 50));
            const { rows, profiles, totalRows, hasMore } = await loadResults(found.state, offset, limit);
            return sendJson(res, {
              run: {
                ...found.summary,
                constraints: found.state.constraints,
                steps: found.state.steps || [],
                artifacts: found.state.artifacts,
                resultCount: totalRows ?? rows.length,
              },
              rows,
              profiles,
              offset,
              limit,
              hasMore,
              totalRows: totalRows ?? rows.length,
            });
          }

          return next();
        } catch (err) {
          console.error("[powerpacks-local-api]", err);
          return sendJson(res, { error: err instanceof Error ? err.message : String(err) }, 500);
        }
      });
    },
  };
}

export default defineConfig(() => ({
  server: {
    host: "0.0.0.0",
    port: 5177,
    strictPort: false,
  },
  plugins: [
    react(),
    powerpacksLocalApiPlugin(),
  ].filter(Boolean),
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  build: {
    commonjsOptions: {
      ignoreTryCatch: false,
    },
  },
}));
