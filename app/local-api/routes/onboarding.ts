import path from "path";
import fs from "fs";
import { spawn, spawnSync } from "child_process";
import { randomUUID } from "crypto";

import {
  accountsPath,
  onboardingV2GmailRunsDir,
  onboardingV2LinkedInRunsDir,
  onboardingV2MessagesRunsDir,
  onboardingV3LinkedInRunsDir,
  powerpacksRepoRoot,
  powerpacksStateRoot,
  setupLedgerPath,
} from "../lib/paths";
import { readJsonSync } from "../lib/fsUtils";
import { setupProcessEnv } from "../lib/env";
import { parseJsonFragment, parseLastJsonFragment } from "../lib/subprocess";
import {
  accountRecords,
  configuredMsgvaultDb,
  discoverMsgvaultAccounts,
  localGmailAccountsFromRecord,
  resolveOperator,
} from "../lib/accounts";
import { messagesLinkStatus, sourceSlug } from "../lib/sources";
import { onboardingV2LinkedInCommand, onboardingV3PipelineCommand } from "../lib/commands";
import { readRequestJson, sendJson } from "../lib/http";
import { setupJobsList, startSetupJob } from "../jobs";
import type { SetupJob, SetupJobStage } from "../lib/types";

function validOnboardingV2RunId(runId: string): boolean {
  return /^[a-zA-Z0-9_-][a-zA-Z0-9_:-]{0,127}$/.test(runId);
}

// Each onboarding-v2 vertical keeps a single status.json/events.jsonl that the
// Python runner overwrites when a new run starts (no per-run-id subdirs).
function onboardingV2RunFilePath(runsDir: string, fileName: "status.json" | "events.jsonl"): string {
  return path.join(runsDir, fileName);
}

function readOnboardingV2Events(runsDir: string): Record<string, any>[] {
  const eventsPath = onboardingV2RunFilePath(runsDir, "events.jsonl");
  if (!fs.existsSync(eventsPath)) return [];
  return fs.readFileSync(eventsPath, "utf8")
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean)
    .slice(-250)
    .map((line) => {
      try {
        return JSON.parse(line) as Record<string, any>;
      } catch {
        return null;
      }
    })
    .filter((event): event is Record<string, any> => Boolean(event));
}

function safeOnboardingV2LinkedInCsvPath(value: unknown): string | undefined {
  const raw = String(value || "").trim();
  if (!raw) return undefined;
  const resolved = path.resolve(powerpacksRepoRoot, raw.replace(/^~(?=\/|$)/, process.env.HOME || ""));
  const allowedUploadDir = `${path.resolve(powerpacksStateRoot, "ingestion", "uploads", "linkedin")}${path.sep}`;
  const stableConnectionsCsv = path.resolve(powerpacksStateRoot, "network-import", "discover", "linkedin", "Connections.csv");
  if (resolved === stableConnectionsCsv || resolved.startsWith(allowedUploadDir)) return resolved;
  throw new Error("LinkedIn CSV path must be the stable local Connections.csv or an uploaded LinkedIn CSV");
}

type OnboardingV2Vertical = {
  vertical: string;
  action: string;
  actionKeyPrefix: string;
  runsDir: string;
  defaultStages: { id: string; label: string }[];
};

const ONBOARDING_V2_LINKEDIN: OnboardingV2Vertical = {
  vertical: "linkedin_csv",
  action: "onboarding-v2-linkedin",
  actionKeyPrefix: "onboarding-v2:linkedin:",
  runsDir: onboardingV2LinkedInRunsDir,
  defaultStages: [
    { id: "inspect", label: "Check LinkedIn CSV" },
    { id: "discover", label: "Import LinkedIn contacts" },
    { id: "enrich", label: "Enrich LinkedIn profiles" },
    { id: "source_people", label: "Save LinkedIn people file" },
    { id: "merge_network", label: "Merge contact sources" },
    { id: "network_duckdb", label: "Prepare contact lookup database" },
    { id: "index_estimate", label: "Estimate search updates" },
    { id: "index_records", label: "Build searchable people records" },
    { id: "search_duckdb", label: "Update local search database" },
  ],
};

const ONBOARDING_V2_GMAIL: OnboardingV2Vertical = {
  vertical: "gmail",
  action: "onboarding-v2-gmail",
  actionKeyPrefix: "onboarding-v2:gmail:",
  runsDir: onboardingV2GmailRunsDir,
  defaultStages: [
    { id: "inspect", label: "Check linked Gmail accounts" },
    { id: "discover", label: "Sync and discover Gmail contacts" },
    { id: "enrich", label: "Enrich Gmail contacts" },
    { id: "source_people", label: "Save Gmail people file" },
    { id: "merge_network", label: "Merge contact sources" },
    { id: "network_duckdb", label: "Prepare contact lookup database" },
    { id: "index_estimate", label: "Estimate search updates" },
    { id: "index_records", label: "Build searchable people records" },
    { id: "search_duckdb", label: "Update local search database" },
  ],
};

const ONBOARDING_V2_MESSAGES: OnboardingV2Vertical = {
  vertical: "messages",
  action: "onboarding-v2-messages",
  actionKeyPrefix: "onboarding-v2:messages:",
  runsDir: onboardingV2MessagesRunsDir,
  defaultStages: [
    { id: "inspect", label: "Check message sources" },
    { id: "discover", label: "Discover message contacts" },
    { id: "llm_review", label: "AI contact review" },
    { id: "user_review", label: "Review contacts" },
    { id: "enrich", label: "Enrich message contacts" },
    { id: "source_people", label: "Save message people file" },
    { id: "merge_network", label: "Merge contact sources" },
    { id: "network_duckdb", label: "Prepare contact lookup database" },
    { id: "index_estimate", label: "Estimate search updates" },
    { id: "index_records", label: "Build searchable people records" },
    { id: "search_duckdb", label: "Update local search database" },
  ],
};

function activeOnboardingV2Job(config: OnboardingV2Vertical, runId: string): SetupJob | null {
  return setupJobsList().find((job) => (
    job.action === config.action
    && job.actionKey === `${config.actionKeyPrefix}${runId}`
    && job.status === "running"
  )) || null;
}

// Any running job for this vertical, regardless of run id. The single-file
// status/events model means a second concurrent run would truncate events.jsonl
// and overwrite status.json out from under the first, so callers reject a new
// run while one is already in flight.
function runningOnboardingV2VerticalJob(config: OnboardingV2Vertical): SetupJob | null {
  return setupJobsList().find((job) => job.action === config.action && job.status === "running") || null;
}

function onboardingV2Status(config: OnboardingV2Vertical) {
  const statusPath = onboardingV2RunFilePath(config.runsDir, "status.json");
  const status = readJsonSync(statusPath) || {
    status: "missing",
    vertical: config.vertical,
    progress: 0,
    stage_order: config.defaultStages,
  };
  const resolvedRunId = String(status.run_id || "");
  // Prefer the job matching the persisted run id; fall back to any running job
  // for this vertical so a freshly started run (before Python overwrites
  // status.json) is not reported as stale/inactive.
  const resolvedActiveJob = (resolvedRunId ? activeOnboardingV2Job(config, resolvedRunId) : null)
    || runningOnboardingV2VerticalJob(config);
  const updatedAt = Date.parse(String(status.updated_at || ""));
  const missingHeartbeat = String(status.status || "") === "running" && !resolvedActiveJob && !Number.isFinite(updatedAt);
  const stale = String(status.status || "") === "running"
    && !resolvedActiveJob
    && (missingHeartbeat || (Number.isFinite(updatedAt) && Date.now() - updatedAt > 10 * 60 * 1000));
  return {
    ...status,
    status_path: fs.existsSync(statusPath) ? path.relative(powerpacksRepoRoot, statusPath) : String(status.status_path || ""),
    events: readOnboardingV2Events(config.runsDir),
    active_job: resolvedActiveJob,
    stale,
    stale_reason: stale ? missingHeartbeat ? "This persisted run is marked running but has no active local API job or heartbeat timestamp." : "No active local API job has updated this persisted run recently. The Python runner may have been killed or the dev server may have restarted." : "",
  };
}

function onboardingV2LinkedInStatus() {
  return onboardingV2Status(ONBOARDING_V2_LINKEDIN);
}

function linkedGmailAccountEmails(): string[] {
  const accounts = readJsonSync(accountsPath) || {};
  const record = accountRecords(accounts).gmail || {};
  return localGmailAccountsFromRecord(record);
}

function onboardingV2GmailStatus() {
  const status = onboardingV2Status(ONBOARDING_V2_GMAIL);
  const persisted = Array.isArray((status as Record<string, any>).linked_accounts)
    ? (status as Record<string, any>).linked_accounts as unknown[]
    : [];
  // Surface linked accounts from accounts.json so the single-button flow can run
  // on first page load without requiring a manual dry-run. The persisted status
  // only carries linked_accounts inside result once a run completes.
  const linkedAccounts = persisted.length > 0 ? persisted.map(String) : linkedGmailAccountEmails();
  // Surface discovered msgvault accounts so the v2 page can offer a connect UI
  // without requiring the user to run the CLI onboarding step first.
  const dbPath = configuredMsgvaultDb(readJsonSync(accountsPath));
  const discovered = discoverMsgvaultAccounts(dbPath);
  // Surface expired accounts from the inspect stage payload so the UI can
  // show per-account re-authorize buttons without parsing error strings.
  const inspectStage = (status as Record<string, any>)?.stages?.inspect || {};
  const inspectPayload = inspectStage.payload || {};
  const expiredAccounts = Array.isArray(inspectPayload.expired_accounts) ? inspectPayload.expired_accounts : [];
  return { ...status, linked_accounts: linkedAccounts, discovered_accounts: discovered.rows, discovered_error: discovered.error || "", expired_accounts: expiredAccounts };
}

function dryRunOnboardingV2LinkedIn(body: Record<string, any>) {
  const setupLedger = readJsonSync(setupLedgerPath) || {};
  const accounts = readJsonSync(accountsPath) || {};
  const operator = resolveOperator(setupLedger, accounts);
  const command = onboardingV2LinkedInCommand("dry-run", operator.id, {
    csvPath: safeOnboardingV2LinkedInCsvPath(body.csvPath),
    sourceLabel: String(body.sourceLabel || "").trim() || undefined,
  });
  return runOnboardingV2DryRunCommand(command);
}

function runOnboardingV2DryRunCommand(command: string[]) {
  const result = spawnSync(command[0], command.slice(1), {
    cwd: powerpacksRepoRoot,
    env: setupProcessEnv(),
    encoding: "utf8",
    maxBuffer: 20 * 1024 * 1024,
    timeout: 5 * 60 * 1000,
  });
  const output = parseLastJsonFragment(result.stdout || "") || {};
  return {
    status: result.status === 0 ? "ok" : "failed",
    code: result.status,
    command,
    stdout: result.stdout || "",
    stderr: result.stderr || "",
    output,
  };
}

function resolveOnboardingV2RunId(body: Record<string, any>): string {
  const runId = sourceSlug(String(body.runId || `local-${Date.now()}-${randomUUID().slice(0, 8)}`)).replace(/[.]+/g, "-");
  if (!validOnboardingV2RunId(runId)) throw new Error("Invalid onboarding run ID");
  return runId;
}

// Onboarding v3: LinkedIn csv -> Modal sandboxes (Importing -> Indexing). The
// Python driver mirrors sandbox progress into the same status.json/events.jsonl
// shape, so the generic v2 status reader works unchanged.
const ONBOARDING_V3_LINKEDIN: OnboardingV2Vertical = {
  vertical: "linkedin_modal",
  action: "onboarding-v3-linkedin",
  actionKeyPrefix: "onboarding-v3:linkedin:",
  runsDir: onboardingV3LinkedInRunsDir,
  defaultStages: [
    { id: "importing", label: "Importing contacts" },
    { id: "indexing", label: "Building search index" },
  ],
};

function startOnboardingV3LinkedIn(body: Record<string, any>): SetupJob {
  const existing = runningOnboardingV2VerticalJob(ONBOARDING_V3_LINKEDIN);
  if (existing) return existing;
  const command = onboardingV3PipelineCommand({
    csvPath: safeOnboardingV2LinkedInCsvPath(body.csvPath) || "",
    sourceLabel: String(body.sourceLabel || "").trim() || undefined,
    force: body.force === true,
  });
  return startSetupJob(ONBOARDING_V3_LINKEDIN.action, command, 6 * 60 * 60 * 1000, {
    source: ONBOARDING_V3_LINKEDIN.vertical,
    stages: onboardingV2JobStages(ONBOARDING_V3_LINKEDIN),
  });
}

function onboardingV2JobStages(config: OnboardingV2Vertical): SetupJobStage[] {
  return config.defaultStages.map((stage, index) => ({ label: stage.label, index: index + 1, total: config.defaultStages.length }));
}

function startOnboardingV2LinkedIn(body: Record<string, any>): SetupJob {
  const setupLedger = readJsonSync(setupLedgerPath) || {};
  const accounts = readJsonSync(accountsPath) || {};
  const operator = resolveOperator(setupLedger, accounts);
  const existing = runningOnboardingV2VerticalJob(ONBOARDING_V2_LINKEDIN);
  if (existing) return existing;
  const command = onboardingV2LinkedInCommand("run", operator.id, {
    csvPath: safeOnboardingV2LinkedInCsvPath(body.csvPath),
    sourceLabel: String(body.sourceLabel || "").trim() || undefined,
    force: body.force === true,
  });
  return startSetupJob(ONBOARDING_V2_LINKEDIN.action, command, 6 * 60 * 60 * 1000, {
    source: ONBOARDING_V2_LINKEDIN.vertical,
    stages: onboardingV2JobStages(ONBOARDING_V2_LINKEDIN),
  });
}

function onboardingV2GmailCommand(command: "dry-run" | "run", operatorId: string, options: { approveSpend?: boolean; maxEnrich?: number; continueRun?: boolean } = {}) {
  const args = [
    "uv", "run", "--project", ".", "python",
    "packs/ingestion/primitives/setup_gmail/setup_gmail.py",
    command,
    "--operator-id", operatorId,
    "--accounts", ".powerpacks/ingestion/accounts.json",
  ];
  if (options.approveSpend) args.push("--approve-spend");
  if (options.maxEnrich && options.maxEnrich > 0) args.push("--max-enrich", String(options.maxEnrich));
  if (options.continueRun) args.push("--continue");
  return args;
}

function dryRunOnboardingV2Gmail() {
  const setupLedger = readJsonSync(setupLedgerPath) || {};
  const accounts = readJsonSync(accountsPath) || {};
  const operator = resolveOperator(setupLedger, accounts);
  const command = onboardingV2GmailCommand("dry-run", operator.id);
  return runOnboardingV2DryRunCommand(command);
}

function startOnboardingV2Gmail(body: Record<string, any>): SetupJob {
  const setupLedger = readJsonSync(setupLedgerPath) || {};
  const accounts = readJsonSync(accountsPath) || {};
  const operator = resolveOperator(setupLedger, accounts);
  const existing = runningOnboardingV2VerticalJob(ONBOARDING_V2_GMAIL);
  if (existing) return existing;
  const approveSpend = body.approveSpend === true;
  const maxEnrich = typeof body.maxEnrich === "number" ? body.maxEnrich : 0;
  const continueRun = body.continueRun === true;
  const command = onboardingV2GmailCommand("run", operator.id, { approveSpend, maxEnrich: maxEnrich || undefined, continueRun });
  return startSetupJob(ONBOARDING_V2_GMAIL.action, command, 6 * 60 * 60 * 1000, {
    source: ONBOARDING_V2_GMAIL.vertical,
    stages: onboardingV2JobStages(ONBOARDING_V2_GMAIL),
  });
}

function onboardingV2MessagesCommand(command: "dry-run" | "run", operatorId: string, options: { approveSpend?: boolean; maxEnrich?: number; continueRun?: boolean } = {}) {
  const args = [
    "uv", "run", "--project", ".", "python",
    "packs/ingestion/primitives/setup_messages/setup_messages.py",
    command,
    "--operator-id", operatorId,
    "--accounts", ".powerpacks/ingestion/accounts.json",
  ];
  if (options.approveSpend) args.push("--approve-spend");
  if (options.maxEnrich && options.maxEnrich > 0) args.push("--max-enrich", String(options.maxEnrich));
  if (options.continueRun) args.push("--continue");
  return args;
}

function onboardingV2MessagesStatus() {
  const status = onboardingV2Status(ONBOARDING_V2_MESSAGES);
  const accounts = readJsonSync(accountsPath) || {};
  const messagesRecord = accountRecords(accounts).messages || {};
  const messagesConfig = messagesRecord.config && typeof messagesRecord.config === "object" ? messagesRecord.config : {};
  const linkStatus = messagesLinkStatus(messagesConfig);
  return { ...status, sources: linkStatus, messages_linked: Boolean(messagesRecord.linked) };
}

function startOnboardingV2Messages(body: Record<string, any>): SetupJob {
  const setupLedger = readJsonSync(setupLedgerPath) || {};
  const accounts = readJsonSync(accountsPath) || {};
  const operator = resolveOperator(setupLedger, accounts);
  const existing = runningOnboardingV2VerticalJob(ONBOARDING_V2_MESSAGES);
  if (existing) return existing;
  const approveSpend = body.approveSpend === true;
  const maxEnrich = typeof body.maxEnrich === "number" ? body.maxEnrich : 0;
  const continueRun = body.continueRun === true;
  const command = onboardingV2MessagesCommand("run", operator.id, { approveSpend, maxEnrich: maxEnrich || undefined, continueRun });
  return startSetupJob(ONBOARDING_V2_MESSAGES.action, command, 6 * 60 * 60 * 1000, {
    source: ONBOARDING_V2_MESSAGES.vertical,
    stages: onboardingV2JobStages(ONBOARDING_V2_MESSAGES),
  });
}

// Onboarding v3 Gmail: estimate how much a date-windowed sync would pull,
// per window, without syncing. Read-only and free (Gmail label/id counts).
// Uses async spawn (never spawnSync) so the Gmail pagination — up to ~30s on a
// large inbox — does not block the single-threaded dev server event loop.
function estimateGmailSync(body: Record<string, any>): Promise<Record<string, any>> {
  const accounts: string[] = Array.isArray(body.accounts) ? body.accounts.map(String).filter(Boolean) : [];
  const windows: string[] = Array.isArray(body.windows) && body.windows.length
    ? body.windows.map(String)
    : ["1y", "2y", "5y", "all"];
  const command = [
    "uv", "run", "--project", ".", "python",
    "packs/ingestion/primitives/estimate_gmail_sync/estimate_gmail_sync.py",
    "estimate",
  ];
  for (const account of accounts) command.push("--account", account);
  for (const window of windows) command.push("--window", window);
  return new Promise((resolve) => {
    const child = spawn(command[0], command.slice(1), { cwd: powerpacksRepoRoot, env: setupProcessEnv() });
    let stdout = "";
    let stderr = "";
    const timer = setTimeout(() => child.kill("SIGKILL"), 5 * 60 * 1000);
    child.stdout.on("data", (chunk) => { stdout += chunk.toString(); });
    child.stderr.on("data", (chunk) => { stderr += chunk.toString(); });
    child.on("error", (err) => {
      clearTimeout(timer);
      resolve({ status: "failed", error: String(err) });
    });
    child.on("close", (code) => {
      clearTimeout(timer);
      const output = parseLastJsonFragment(stdout) || {};
      if (code === 0 && (output as Record<string, any>).status === "completed") resolve(output);
      else resolve({ status: "failed", code, error: stderr || "estimate failed", output });
    });
  });
}

export async function handleOnboardingV2Routes(req: any, res: any, url: URL): Promise<boolean> {
  if (url.pathname === "/local-api/onboarding-v3/gmail/estimate" && req.method === "POST") {
    sendJson(res, await estimateGmailSync(await readRequestJson(req)));
    return true;
  }

  if (url.pathname === "/local-api/onboarding-v3/linkedin/status") {
    sendJson(res, onboardingV2Status(ONBOARDING_V3_LINKEDIN));
    return true;
  }

  if (url.pathname === "/local-api/onboarding-v3/linkedin/run" && req.method === "POST") {
    const job = startOnboardingV3LinkedIn(await readRequestJson(req));
    sendJson(res, { job, status: onboardingV2Status(ONBOARDING_V3_LINKEDIN) });
    return true;
  }

  if (url.pathname === "/local-api/onboarding-v2/linkedin/status") {
    sendJson(res, onboardingV2LinkedInStatus());
    return true;
  }

  if (url.pathname === "/local-api/onboarding-v2/linkedin/dry-run" && req.method === "POST") {
    sendJson(res, dryRunOnboardingV2LinkedIn(await readRequestJson(req)));
    return true;
  }

  if (url.pathname === "/local-api/onboarding-v2/linkedin/run" && req.method === "POST") {
    const job = startOnboardingV2LinkedIn(await readRequestJson(req));
    sendJson(res, { job, status: onboardingV2LinkedInStatus() });
    return true;
  }

  if (url.pathname === "/local-api/onboarding-v2/gmail/status") {
    sendJson(res, onboardingV2GmailStatus());
    return true;
  }

  if (url.pathname === "/local-api/onboarding-v2/gmail/check-tokens" && req.method === "POST") {
    const body = await readRequestJson(req);
    const emails = Array.isArray(body.emails) ? body.emails.map(String).filter(Boolean) : [];
    if (emails.length === 0) {
      sendJson(res, { expired: [] });
      return true;
    }
    const command = [
      "uv", "run", "--project", ".", "python", "-c",
      `import json; from packs.ingestion.primitives.setup_gmail.setup_gmail import _check_gmail_tokens; print(json.dumps({"expired": _check_gmail_tokens(${JSON.stringify(emails)})}))`,
    ];
    const result = spawnSync(command[0], command.slice(1), {
      cwd: powerpacksRepoRoot, env: setupProcessEnv(), encoding: "utf8", timeout: 60000,
    });
    const payload = parseJsonFragment(result.stdout || "") || { expired: emails };
    sendJson(res, payload);
    return true;
  }

  if (url.pathname === "/local-api/onboarding-v2/gmail/dry-run" && req.method === "POST") {
    sendJson(res, dryRunOnboardingV2Gmail());
    return true;
  }

  if (url.pathname === "/local-api/onboarding-v2/gmail/run" && req.method === "POST") {
    const job = startOnboardingV2Gmail(await readRequestJson(req));
    sendJson(res, { job, status: onboardingV2GmailStatus() });
    return true;
  }

  if (url.pathname === "/local-api/onboarding-v2/messages/status") {
    sendJson(res, onboardingV2MessagesStatus());
    return true;
  }

  if (url.pathname === "/local-api/onboarding-v2/messages/run" && req.method === "POST") {
    const job = startOnboardingV2Messages(await readRequestJson(req));
    sendJson(res, { job, status: onboardingV2MessagesStatus() });
    return true;
  }

  return false;
}
