import path from "path";
import fs from "fs";

import { setupJobs, startSetupJob } from "../jobs";
import { powerpacksRepoRoot, safeJoinPowerpacks } from "../lib/paths";
import { readJsonSync } from "../lib/fsUtils";
import { readRequestJson, sendJson } from "../lib/http";
import { shellJoin } from "../lib/shell";

const LOCAL_DB_RELATIVE = ".powerpacks/search-index/local-search.duckdb";
const PIPELINE_SCRIPT = "packs/search/primitives/local_search_pipeline/local_search_pipeline.py";

// In-memory association between a setup job and the search run it drives.
// Mirrors the in-memory setupJobs map: no on-disk state beyond what the
// pipeline itself writes (its ledger + the .powerpacks/runs task state).
type LocalSearchRun = {
  jobId: string;
  query: string;
  outputDir: string;
  ledgerPath: string;
  startedAt: string;
};

const localSearchRuns = new Map<string, LocalSearchRun>();

function pruneLocalSearchRuns() {
  const runs = [...localSearchRuns.values()].sort((a, b) => b.startedAt.localeCompare(a.startedAt));
  for (const run of runs.slice(40)) localSearchRuns.delete(run.jobId);
}

function slugify(value: string): string {
  return value.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 48) || "query";
}

function pipelineCommand(subcommand: "prepare" | "run", args: string[]): string[] {
  return ["uv", "run", "--project", ".", "python", PIPELINE_SCRIPT, subcommand, ...args];
}

function tailOf(text: string, maxChars = 4000): string {
  const trimmed = (text || "").trim();
  return trimmed.length > maxChars ? trimmed.slice(-maxChars) : trimmed;
}

type LedgerStep = { id: string; status: string };

function readLedgerSteps(ledgerPath: string): { steps: LedgerStep[]; statePath: string | null } {
  const ledger = readJsonSync(ledgerPath);
  if (!ledger || typeof ledger !== "object") return { steps: [], statePath: null };
  const steps = Object.entries(ledger.steps || {}).map(([id, value]: [string, any]) => ({
    id,
    status: String(value?.status || "unknown"),
  }));
  const statePath = typeof ledger.state === "string" && ledger.state ? ledger.state : null;
  return { steps, statePath };
}

function taskIdsFromState(statePath: string | null): { taskId: string | null; conversationId: string | null } {
  if (!statePath) return { taskId: null, conversationId: null };
  const resolved = path.isAbsolute(statePath) ? statePath : path.join(powerpacksRepoRoot, statePath);
  const state = readJsonSync(resolved);
  const taskId = String(state?.task_id || path.basename(resolved, ".json"));
  if (!taskId) return { taskId: null, conversationId: null };
  const conversationId = taskId.replace(/^search-network-/, "").match(/^[0-9a-f-]{36}/i)?.[0] || taskId;
  return { taskId, conversationId };
}

function jobStatusPayload(jobId: string) {
  const job = setupJobs.get(jobId);
  if (!job) return null;
  const run = localSearchRuns.get(jobId);
  const { steps, statePath } = run ? readLedgerSteps(run.ledgerPath) : { steps: [], statePath: null };
  const { taskId, conversationId } = taskIdsFromState(statePath);
  const outputError = typeof job.output?.error === "string" ? job.output.error : null;
  return {
    jobId,
    status: job.status,
    query: run?.query ?? null,
    startedAt: job.startedAt,
    completedAt: job.completedAt ?? null,
    taskId,
    conversationId,
    steps,
    summary: job.output?.summary ?? null,
    error: job.status === "failed" || job.status === "blocked"
      ? outputError || tailOf(job.stderr || "", 1200) || "local search failed"
      : null,
    logTail: tailOf(job.log || ""),
  };
}

async function handleStart(req: any, res: any): Promise<void> {
  let body: Record<string, any>;
  try {
    body = await readRequestJson(req);
  } catch {
    sendJson(res, { error: "invalid JSON body" }, 400);
    return;
  }

  const query = String(body.query || "").trim();
  if (!query) {
    sendJson(res, { error: "query is required" }, 400);
    return;
  }

  const dbPath = path.join(powerpacksRepoRoot, LOCAL_DB_RELATIVE);
  if (!fs.existsSync(dbPath)) {
    sendJson(res, {
      error: `local search index not found at ${LOCAL_DB_RELATIVE}; run $build-local-search-index first`,
    }, 409);
    return;
  }

  // Optional plumbing-test inputs: a prebuilt expansion payload (skips the
  // OpenAI query-expansion call) and search-only mode (skips LLM
  // filter/rerank). The console UI never sends these; they exist so the
  // end-to-end job/run-dir path can be exercised without LLM spend.
  let payloadJsonPath: string | null = null;
  if (body.payloadJsonPath) {
    payloadJsonPath = safeJoinPowerpacks(String(body.payloadJsonPath));
    if (!payloadJsonPath || !fs.existsSync(payloadJsonPath)) {
      sendJson(res, { error: "payloadJsonPath must be an existing file under the repo root" }, 400);
      return;
    }
  }
  const searchOnly = body.searchOnly === true;

  const runDirName = `console-${Date.now().toString(36)}-${slugify(query)}`;
  const outputDir = path.join(".powerpacks", "search", runDirName);
  const ledgerRelative = path.join(outputDir, "local-search.pipeline.json");
  const payloadRelative = payloadJsonPath
    ? path.relative(powerpacksRepoRoot, payloadJsonPath)
    : path.join(outputDir, "expand_search_request.local.json");

  const runArgs = [
    "--db", LOCAL_DB_RELATIVE,
    "--ledger", ledgerRelative,
    "--query", query,
    "--payload-json", payloadRelative,
    ...(searchOnly ? ["--search-only"] : []),
  ];

  const stages: string[] = [];
  if (!payloadJsonPath) {
    stages.push(shellJoin(pipelineCommand("prepare", ["--query", query, "--db", LOCAL_DB_RELATIVE, "--output-dir", outputDir])));
  }
  stages.push(shellJoin(pipelineCommand("run", runArgs)));
  const script = stages.join(" && ");

  pruneLocalSearchRuns();
  const job = startSetupJob("local-search", ["/bin/zsh", "-lc", script], 30 * 60 * 1000, {
    actionKey: "local-search",
    source: "search",
  });
  localSearchRuns.set(job.id, {
    jobId: job.id,
    query,
    outputDir,
    ledgerPath: path.join(powerpacksRepoRoot, ledgerRelative),
    startedAt: job.startedAt,
  });

  sendJson(res, { jobId: job.id, status: job.status, ledger: ledgerRelative }, 202);
}

export async function handleLocalSearchRoutes(req: any, res: any, url: URL): Promise<boolean> {
  if (url.pathname === "/local-api/search/local-run" && req.method === "POST") {
    await handleStart(req, res);
    return true;
  }

  const statusMatch = url.pathname.match(/^\/local-api\/search\/local-run\/([^/]+)$/);
  if (statusMatch && req.method === "GET") {
    const payload = jobStatusPayload(decodeURIComponent(statusMatch[1]));
    if (!payload) {
      sendJson(res, { error: "job not found" }, 404);
      return true;
    }
    sendJson(res, payload);
    return true;
  }

  return false;
}
