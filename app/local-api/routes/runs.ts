import path from "path";
import fs from "fs";
import fsp from "fs/promises";

import { runsDir, safeJoinPowerpacks } from "../lib/paths";
import { readJsonSync } from "../lib/fsUtils";
import { readCsvWindow, readJsonlForIds, readJsonlWindow, readProfilesForIds } from "../lib/csv";
import { sendJson } from "../lib/http";
import type { RunState } from "../lib/types";

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

async function loadResults(state: RunState, offset = 0, limit = 50) {
  const artifacts = state.artifacts || {};
  const resultsCsv = safeJoinPowerpacks(artifacts.csv);
  const jsonlPath = safeJoinPowerpacks(artifacts.jsonl);
  const artifactDir = safeJoinPowerpacks(artifacts.artifact_dir);
  const rerankCsv = artifactDir ? path.join(artifactDir, "llm_rerank_candidates", "query_results.csv") : null;

  let rows: any[] = [];
  let totalRows = Number(artifacts.row_count ?? 0) || null;

  if (resultsCsv && fs.existsSync(resultsCsv)) {
    const { rows: resultRows, total } = await readCsvWindow(resultsCsv, offset, limit);
    totalRows = total;
    rows = resultRows.map((row) => ({
      ...row,
      rank: Number(row.rank ?? 0),
      reranked: row.final_score != null && row.final_score !== "",
    }));
  } else if (rerankCsv && fs.existsSync(rerankCsv)) {
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

export async function handleRunsRoutes(req: any, res: any, url: URL): Promise<boolean> {
  if (url.pathname === "/local-api/runs") {
    sendJson(res, await listRuns());
    return true;
  }

  const match = url.pathname.match(/^\/local-api\/runs\/([^/]+)\/results$/);
  if (match) {
    const taskId = decodeURIComponent(match[1]);
    const found = await findRun(taskId);
    if (!found) {
      sendJson(res, { error: "Run not found" }, 404);
      return true;
    }
    const offset = Math.max(0, Number(url.searchParams.get("offset") || 0) || 0);
    const limit = Math.min(200, Math.max(1, Number(url.searchParams.get("limit") || 50) || 50));
    const { rows, profiles, totalRows, hasMore } = await loadResults(found.state, offset, limit);
    sendJson(res, {
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
    return true;
  }

  return false;
}
