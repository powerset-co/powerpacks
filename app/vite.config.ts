import { defineConfig, type Plugin } from "vite";
import react from "@vitejs/plugin-react-swc";
import path from "path";
import fs from "fs";
import fsp from "fs/promises";
import { spawn, spawnSync } from "child_process";
import { createHash, randomUUID } from "crypto";
import { createGunzip } from "zlib";
import { createInterface } from "readline";

const powerpacksRepoRoot = path.resolve(
  __dirname,
  process.env.POWERPACKS_REPO_ROOT || ".."
);
const powerpacksStateRoot = path.join(powerpacksRepoRoot, ".powerpacks");
const runsDir = path.join(powerpacksStateRoot, "runs");
const setupLedgerPath = path.join(powerpacksStateRoot, "setup", "setup-run.json");
const accountsPath = path.join(powerpacksStateRoot, "ingestion", "accounts.json");
const importRefreshLedgerPath = path.join(powerpacksStateRoot, "network-import", "import-network-run.setup-refresh.json");
const messagesLedgerPath = path.join(powerpacksStateRoot, "messages", "import-run.setup-messages.json");
const messagesReviewCsvPath = path.join(powerpacksStateRoot, "messages", "research_review.csv");
const messagesChatDbPath = process.env.POWERPACKS_IMESSAGE_CHAT_DB
  ? path.resolve(process.env.POWERPACKS_IMESSAGE_CHAT_DB)
  : path.join(process.env.HOME || "", "Library", "Messages", "chat.db");
const whatsAppStorePath = ".powerpacks/messages/wacli";

type RunState = Record<string, any>;
type CsvDocument = { headers: string[]; rows: Record<string, string>[] };
type SetupJob = {
  id: string;
  action: string;
  status: "running" | "completed" | "failed" | "blocked";
  startedAt: string;
  completedAt?: string | null;
  command: string[];
  code?: number | null;
  stdout?: string;
  stderr?: string;
  output?: Record<string, any> | null;
};
type SetupImportSource = {
  id: string;
  sourceId: string;
  label: string;
  status: string;
  linked: boolean;
  skipped: boolean;
  accountEmail?: string;
  accountCount?: number;
  runnable?: boolean;
  disabledReason?: string;
  updatedAt?: string | null;
  runId?: string;
  command: string[];
};
type SetupEnrichmentSource = {
  id: string;
  label: string;
  status: string;
  candidates: number;
  enriched: number;
  skipped: number;
  matched: number;
  updatedAt?: string | null;
};
type SetupOperator = { id: string; email?: string; label: string };

function summarizeEnrichmentStatus(sources: SetupEnrichmentSource[]): string {
  if (sources.some((source) => String(source.status).startsWith("blocked"))) return "blocked_user_action";
  if (sources.some((source) => source.status === "running")) return "running";
  if (sources.some((source) => source.enriched > 0)) return "completed";
  if (sources.some((source) => source.candidates > 0)) return "ready";
  return "unknown";
}

const setupJobs = new Map<string, SetupJob>();
let cachedWhatsAppLinkStatus: { expiresAt: number; value: Record<string, any> } | null = null;
let cachedDuckdbTables: { key: string; expiresAt: number; value: Array<{ name: string; rows: number }> } | null = null;
let cachedIndexEstimate: { key: string; expiresAt: number; value: Record<string, any> } | null = null;
const SETUP_SOURCE_ORDER = ["gmail", "linkedin_csv", "messages", "twitter"] as const;
const SETUP_SOURCE_LABELS: Record<string, string> = {
  gmail: "Gmail",
  linkedin_csv: "LinkedIn",
  messages: "Messages",
  twitter: "Twitter/X",
};

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

function writeJsonSync(filePath: string, data: unknown) {
  fs.mkdirSync(path.dirname(filePath), { recursive: true });
  const tmp = `${filePath}.${process.pid}.${Date.now()}.tmp`;
  fs.writeFileSync(tmp, `${JSON.stringify(data, null, 2)}\n`, "utf8");
  fs.renameSync(tmp, filePath);
}

function readEnvSummary(): Record<string, string> {
  const envPath = path.join(powerpacksRepoRoot, ".env");
  if (!fs.existsSync(envPath)) return {};
  const out: Record<string, string> = {};
  for (const line of fs.readFileSync(envPath, "utf8").split(/\r?\n/)) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith("#")) continue;
    const index = trimmed.indexOf("=");
    if (index <= 0) continue;
    const key = trimmed.slice(0, index).trim();
    const value = trimmed.slice(index + 1).trim().replace(/^["']|["']$/g, "");
    out[key] = value;
  }
  return out;
}

function setupProcessEnv(): NodeJS.ProcessEnv {
  return {
    ...readEnvSummary(),
    ...process.env,
    POWERPACKS_REPO_ROOT: powerpacksRepoRoot,
  };
}

function resolveOperator(setupLedger: RunState | null, accounts: RunState | null): SetupOperator {
  const restoreManifest = readJsonSync(path.join(powerpacksStateRoot, "operator-bootstrap", "restore-manifest.json"));
  const latestSync = readJsonSync(path.join(powerpacksStateRoot, "operator-bootstrap", "registry", "latest-sync.json"));
  const searchManifest = readJsonSync(path.join(powerpacksStateRoot, "search-index", "manifest.json"));
  const env = readEnvSummary();
  const candidates = [
    setupLedger?.operator_id,
    accounts?.operator_id || accounts?.operatorId,
    restoreManifest?.operator_id,
    process.env.POWERPACKS_OPERATOR_ID,
    env.POWERPACKS_OPERATOR_ID,
  ];
  const found = candidates.find((candidate) => typeof candidate === "string" && candidate.trim());
  const syncSource = Array.isArray(latestSync?.operator_resolution?.sources) ? latestSync.operator_resolution.sources[0] : null;
  const email = String(
    searchManifest?.operator_email
      || syncSource?.email
      || latestSync?.operator
      || env.POWERPACKS_OPERATOR_EMAIL
      || ""
  ).trim();
  return {
    id: String(found || "local"),
    email: email || undefined,
    label: email || "Local operator",
  };
}

function setupCommandArgs(operatorId: string, phase: "status" | "next" | "bootstrap" | "link" | "import" | "fan-in" | "index" | "run", extra: string[] = []) {
  return [
    "uv", "run", "--project", ".", "python",
    "packs/ingestion/primitives/setup/setup.py",
    phase,
    "--operator-id", operatorId,
    "--accounts", ".powerpacks/ingestion/accounts.json",
    "--setup-ledger", ".powerpacks/setup/setup-run.json",
    ...extra,
  ];
}

function shellQuote(value: string): string {
  return `'${String(value).replace(/'/g, `'\\''`)}'`;
}

function shellJoin(command: string[]): string {
  return command.map(shellQuote).join(" ");
}

function normalizeEmailList(value: unknown): string[] {
  const values = Array.isArray(value)
    ? value.flatMap((item) => String(item).split(/[,\n\s]+/))
    : String(value || "").split(/[,\n\s]+/);
  return [...new Set(values.map((item) => item.trim().toLowerCase()).filter(Boolean))];
}

function gmailOauthProjectId(email: string): string {
  const digest = createHash("sha1").update(email.trim().toLowerCase()).digest("hex").slice(0, 14);
  return `local-msg-vault-${digest}`;
}

function msgvaultHomeArgs(): string[] {
  const configured = process.env.MSGVAULT_HOME ? path.resolve(process.env.MSGVAULT_HOME) : "";
  const defaultHome = path.join(process.env.HOME || "", ".msgvault");
  return configured && configured !== defaultHome ? ["--home", configured] : [];
}

function msgvaultOauthConfigured(): boolean {
  const home = process.env.MSGVAULT_HOME ? path.resolve(process.env.MSGVAULT_HOME) : path.join(process.env.HOME || "", ".msgvault");
  const config = path.join(home, "config.toml");
  try {
    return fs.readFileSync(config, "utf8").includes("client_secrets");
  } catch {
    return false;
  }
}

function gmailLinkCommand(operatorId: string, rawEmails: unknown): string[] {
  const emails = normalizeEmailList(rawEmails);
  if (emails.length === 0) throw new Error("at least one Gmail email is required");
  for (const email of emails) {
    if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email)) throw new Error(`invalid email: ${email}`);
  }

  const setupAdd = setupCommandArgs(operatorId, "link", emails.flatMap((email) => ["--gmail-add-email", email]));
  const setupAuthorized = setupCommandArgs(operatorId, "link", emails.flatMap((email) => ["--gmail-authorized-email", email]));
  const homeArgs = msgvaultHomeArgs();
  const automation: string[][] = [];
  const oauthConfigured = msgvaultOauthConfigured();

  if (!oauthConfigured) {
    const [first, ...rest] = emails;
    automation.push([
      "uv", "run", "--project", ".", "python",
      "packs/ingestion/primitives/msgvault_setup/msgvault_setup.py",
      "browser-setup",
      "--email", first,
      "--project", gmailOauthProjectId(first),
      "--add-account",
      ...homeArgs,
    ]);
    if (rest.length) {
      automation.push([
        "uv", "run", "--project", ".", "python",
        "packs/ingestion/primitives/msgvault_setup/msgvault_setup.py",
        "add-test-users",
        ...rest,
        ...homeArgs,
      ]);
      for (const email of rest) {
        automation.push([
          "uv", "run", "--project", ".", "python",
          "packs/ingestion/primitives/msgvault_setup/msgvault_setup.py",
          "add-account",
          "--email", email,
          ...homeArgs,
        ]);
      }
    }
  } else {
    automation.push([
      "uv", "run", "--project", ".", "python",
      "packs/ingestion/primitives/msgvault_setup/msgvault_setup.py",
      "add-test-users",
      ...emails,
      ...homeArgs,
    ]);
    for (const email of emails) {
      automation.push([
        "uv", "run", "--project", ".", "python",
        "packs/ingestion/primitives/msgvault_setup/msgvault_setup.py",
        "add-account",
        "--email", email,
        ...homeArgs,
      ]);
    }
  }

  const recordPending = `${shellJoin(setupAdd)}; code=$?; if [[ $code -ne 0 && $code -ne 20 ]]; then exit $code; fi`;
  return ["/bin/zsh", "-lc", [recordPending, ...automation.map(shellJoin), shellJoin(setupAuthorized)].join(" && ")];
}

function findBootstrapOperatorSummary(operator: SetupOperator): Record<string, any> {
  const summary = readJsonSync(path.join(powerpacksStateRoot, "operator-bootstrap", "registry", "summary.json")) || {};
  const operators = Array.isArray(summary.operators) ? summary.operators : [];
  return operators.find((item: any) => item?.operator_id === operator.id || item?.operator === operator.label || item?.operator === operator.email)
    || operators.find((item: any) => String(item?.gcs?.bundle || "").includes(`/operators/${operator.id}/`))
    || {};
}

function bootstrapSummary(operator: SetupOperator) {
  const latest = readJsonSync(path.join(powerpacksStateRoot, "operator-bootstrap", "registry", "latest-sync.json")) || {};
  const operatorSummary = findBootstrapOperatorSummary(operator);
  const processing = operatorSummary.processing_counts || {};
  const importCounts = operatorSummary.import_counts || {};
  const bundle = latest.bundle || operatorSummary.bundle || "";
  const hasRecords = Number(processing.people_records || 0) > 0;
  return {
    status: bundle ? "available" : "missing",
    bundle,
    mode: hasRecords && !Object.keys(importCounts).length ? "records only" : hasRecords ? "records plus import checkpoints" : "",
    bundleSha256: latest.bundle_sha256 || latest.bundle_download?.sha256 || "",
    peopleRecords: Number(processing.people_records || 0) || undefined,
    selectedPeople: Number(processing.selected_people || 0) || undefined,
    selectedPositions: Number(processing.selected_positions || 0) || undefined,
    linkedinCount: Number(processing.linkedin_counts || 0) || undefined,
    twitterCount: Number(processing.x_twitter_counts || 0) || undefined,
    companyRecords: Number(processing.companies_records || 0) || undefined,
  };
}

function parseJsonFragment(text: string): Record<string, any> | null {
  for (let i = 0; i < text.length; i += 1) {
    const char = text[i];
    if (char !== "{" && char !== "[") continue;
    try {
      const parsed = JSON.parse(text.slice(i));
      return typeof parsed === "object" && parsed ? parsed as Record<string, any> : { payload: parsed };
    } catch {
      continue;
    }
  }
  return null;
}

function parseLastJsonFragment(text: string): Record<string, any> | null {
  let start = -1;
  let inString = false;
  let escaped = false;
  const stack: string[] = [];
  let last: Record<string, any> | null = null;

  for (let i = 0; i < text.length; i += 1) {
    const char = text[i];
    if (inString) {
      if (escaped) {
        escaped = false;
      } else if (char === "\\") {
        escaped = true;
      } else if (char === "\"") {
        inString = false;
      }
      continue;
    }
    if (char === "\"") {
      inString = true;
      continue;
    }
    if (char === "{" || char === "[") {
      if (stack.length === 0) start = i;
      stack.push(char === "{" ? "}" : "]");
      continue;
    }
    if ((char === "}" || char === "]") && stack.length > 0 && stack[stack.length - 1] === char) {
      stack.pop();
      if (stack.length === 0 && start >= 0) {
        try {
          const parsed = JSON.parse(text.slice(start, i + 1));
          if (typeof parsed === "object" && parsed) last = parsed as Record<string, any>;
        } catch {
          // Logs can contain braces that are not JSON payloads.
        }
        start = -1;
      }
    }
  }

  return last || parseJsonFragment(text);
}

function shellStage(label: string, command: string[]): string {
  return `printf '%s\\n' ${shellQuote(`setup: ${label}`)} && ${shellJoin(command)}`;
}

function importAndFanInCommand(importCommand: string[], fanInCommand: string[], label: string): string[] {
  const importNetworkApprove = [
    "uv", "run", "--project", ".", "python",
    "packs/ingestion/primitives/import_network_pipeline/import_network_pipeline.py",
    "approve",
    "--ledger", importRefreshLedgerPath,
  ];
  const importNetworkContinue = [
    "uv", "run", "--project", ".", "python",
    "packs/ingestion/primitives/import_network_pipeline/import_network_pipeline.py",
    "continue",
    "--ledger", importRefreshLedgerPath,
  ];
  return [
    "/bin/zsh",
    "-lc",
    [
      `printf '%s\\n' ${shellQuote(`setup: ${label}`)}`,
      `${shellJoin(importCommand)}; code=$?`,
      [
        "if [[ $code -eq 20 ]]; then",
        `  printf '%s\\n' ${shellQuote("setup: approving network import spend")}`,
        `  ${shellJoin(importNetworkApprove)} && ${shellJoin(importNetworkContinue)}`,
        "elif [[ $code -ne 0 ]]; then",
        "  exit $code",
        "fi",
      ].join("\n"),
      shellStage("merging local network", fanInCommand),
    ].join(" && "),
  ];
}

function fileSummary(filePath: string) {
  if (!fs.existsSync(filePath)) {
    return { path: path.relative(powerpacksRepoRoot, filePath), exists: false };
  }
  const stat = fs.statSync(filePath);
  return {
    path: path.relative(powerpacksRepoRoot, filePath),
    exists: true,
    updatedAt: stat.mtime.toISOString(),
    sizeBytes: stat.size,
  };
}

function pruneSetupJobs() {
  const jobs = [...setupJobs.values()].sort((a, b) => b.startedAt.localeCompare(a.startedAt));
  for (const job of jobs.slice(40)) setupJobs.delete(job.id);
}

function startSetupJob(action: string, command: string[], timeoutMs = 6 * 60 * 60 * 1000): SetupJob {
  pruneSetupJobs();
  const job: SetupJob = {
    id: randomUUID(),
    action,
    status: "running",
    startedAt: new Date().toISOString(),
    completedAt: null,
    command,
    code: null,
    stdout: "",
    stderr: "",
    output: null,
  };
  setupJobs.set(job.id, job);

  const child = spawn(command[0], command.slice(1), {
    cwd: powerpacksRepoRoot,
    env: setupProcessEnv(),
    shell: false,
  });
  const timer = setTimeout(() => {
    job.stderr = `${job.stderr || ""}\nTimed out after ${Math.round(timeoutMs / 1000)}s`;
    child.kill("SIGTERM");
  }, timeoutMs);

  child.stdout.on("data", (chunk) => {
    job.stdout = `${job.stdout || ""}${chunk.toString()}`;
  });
  child.stderr.on("data", (chunk) => {
    job.stderr = `${job.stderr || ""}${chunk.toString()}`;
  });
  child.on("error", (err) => {
    clearTimeout(timer);
    job.status = "failed";
    job.completedAt = new Date().toISOString();
    job.stderr = `${job.stderr || ""}${err.message}`;
  });
  child.on("close", (code) => {
    clearTimeout(timer);
    job.code = code;
    job.completedAt = new Date().toISOString();
    job.output = parseLastJsonFragment(job.stdout || "");
    const outputStatus = String(job.output?.status || "").toLowerCase();
    job.status = code === 0
      ? "completed"
      : code === 20 || code === 21 || outputStatus.startsWith("blocked")
        ? "blocked"
        : "failed";
  });

  return job;
}

function whitelistedShellCommand(command: string): boolean {
  const trimmed = command.trim();
  return [
    "uv run --project . python packs/ingestion/primitives/msgvault_setup/msgvault_setup.py ",
    "uv run --project . python packs/ingestion/primitives/onboarding/onboarding.py step",
    "uv run --project . python packs/ingestion/primitives/setup/setup.py ",
    "uv run --project . python packs/messages/primitives/import_contacts_pipeline/import_contacts_pipeline.py ",
    "uv run --project . python packs/messages/primitives/import_whatsapp_wacli/import_whatsapp_wacli.py auth",
    "uv run --project . python packs/messages/primitives/extract_imessage_contacts/extract_imessage_contacts.py open-privacy-settings",
  ].some((prefix) => trimmed.startsWith(prefix));
}

function startWhitelistedShellJob(command: string): SetupJob {
  if (!whitelistedShellCommand(command)) {
    throw new Error("Command is not allowed from the local setup UI");
  }
  return startSetupJob("run-command", ["/bin/zsh", "-lc", command]);
}

function setupJobsList(): SetupJob[] {
  return [...setupJobs.values()].sort((a, b) => b.startedAt.localeCompare(a.startedAt));
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

function parseCsvDocument(text: string): CsvDocument {
  const records: string[][] = [];
  let row: string[] = [];
  let current = "";
  let inQuotes = false;

  for (let i = 0; i < text.length; i += 1) {
    const char = text[i];
    if (char === '"') {
      if (inQuotes && text[i + 1] === '"') {
        current += '"';
        i += 1;
      } else {
        inQuotes = !inQuotes;
      }
    } else if (char === "," && !inQuotes) {
      row.push(current);
      current = "";
    } else if ((char === "\n" || char === "\r") && !inQuotes) {
      if (char === "\r" && text[i + 1] === "\n") i += 1;
      row.push(current);
      if (row.some((value) => value.length > 0)) records.push(row);
      row = [];
      current = "";
    } else {
      current += char;
    }
  }

  if (current.length > 0 || row.length > 0) {
    row.push(current);
    if (row.some((value) => value.length > 0)) records.push(row);
  }

  const headers = records[0] || [];
  const rows = records.slice(1).map((values) => {
    const out: Record<string, string> = {};
    headers.forEach((header, i) => {
      out[header] = values[i] ?? "";
    });
    return out;
  });
  return { headers, rows };
}

function resolveReviewCsvPath(): string | null {
  if (fs.existsSync(messagesReviewCsvPath)) return messagesReviewCsvPath;
  const ledger = readJsonSync(messagesLedgerPath) || {};
  const artifactPath = ledger.artifacts?.research_review_csv || ledger.artifacts?.review_csv;
  const resolved = safeJoinPowerpacks(artifactPath);
  if (resolved && fs.existsSync(resolved)) return resolved;
  return fs.existsSync(messagesReviewCsvPath) ? messagesReviewCsvPath : null;
}

function messagesReviewPrimitive(command: string, args: string[] = []): Record<string, any> {
  const csvPath = resolveReviewCsvPath() || messagesReviewCsvPath;
  const result = spawnSync("uv", [
    "run", "--project", ".", "python",
    "packs/messages/primitives/review_research_web/review_research_web.py",
    command,
    "--csv", csvPath,
    "--research-dir", path.join(powerpacksStateRoot, "messages", "research"),
    ...args,
  ], {
    cwd: powerpacksRepoRoot,
    env: setupProcessEnv(),
    encoding: "utf8",
    maxBuffer: 16 * 1024 * 1024,
  });
  if (result.status !== 0) {
    throw new Error((result.stderr || result.stdout || `review primitive failed: ${command}`).trim());
  }
  return JSON.parse(result.stdout || "{}");
}

function messagesReviewResponse(filter = "all", query = "", offset = 0, limit = 100): Record<string, any> {
  return messagesReviewPrimitive("json", [
    "--filter", filter,
    "--query", query,
    "--offset", String(offset),
    "--limit", String(limit),
  ]);
}

function messagesCurrentBlockForUi(messagesLedger: Record<string, any>, reviewCounts: Record<string, any>) {
  const block = messagesLedger.current_block || null;
  if (!block) return null;
  const approvalType = String(block.approval_type || "").trim().toLowerCase();
  if (approvalType === "upload") return null;
  if (approvalType !== "parallel") return block;

  const prepareInput = String(messagesLedger.steps?.prepare_research_queue?.summary?.input || "");
  const selectedForResearch = Number(reviewCounts.researchSelected || 0);

  // Once the setup app owns review decisions, approvals from the old
  // contacts.csv queue are stale. Completing review recomputes the queue from
  // research_review.csv and only then can a fresh Parallel approval be shown.
  if (prepareInput && !prepareInput.includes("research_review.csv")) return null;
  if (selectedForResearch === 0) return null;
  return block;
}

async function readRequestText(req: any): Promise<string> {
  const chunks: Buffer[] = [];
  for await (const chunk of req) {
    chunks.push(Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk));
  }
  return Buffer.concat(chunks).toString("utf8");
}

async function readRequestJson(req: any): Promise<Record<string, any>> {
  const text = await readRequestText(req);
  if (!text.trim()) return {};
  return JSON.parse(text);
}

function accountRecords(accounts: RunState | null): Record<string, any> {
  const records = accounts?.accounts || accounts?.channels || accounts?.sources || {};
  return records && typeof records === "object" ? records : {};
}

function uniqueStrings(values: unknown[]): string[] {
  return [...new Set(values.map((value) => String(value || "").trim().toLowerCase()).filter(Boolean))];
}

function configuredMsgvaultDb(accounts: RunState | null): string {
  const gmail = accountRecords(accounts).gmail || {};
  const config = gmail.config && typeof gmail.config === "object" ? gmail.config : {};
  const configured = String(config.msgvault_db || "").trim();
  if (configured) return path.resolve(configured.replace(/^~(?=\/|$)/, process.env.HOME || ""));
  const home = process.env.MSGVAULT_HOME ? path.resolve(process.env.MSGVAULT_HOME) : path.join(process.env.HOME || "", ".msgvault");
  return path.join(home, "msgvault.db");
}

function localGmailAccountsFromRecord(record: Record<string, any>): string[] {
  const config = record.config && typeof record.config === "object" ? record.config : {};
  return uniqueStrings([
    ...((Array.isArray(config.selected_accounts) ? config.selected_accounts : []) as unknown[]),
    ...((Array.isArray(config.account_emails) ? config.account_emails : []) as unknown[]),
    ...((Array.isArray(record.usernames) ? record.usernames : []) as unknown[]),
  ]);
}

function shouldAutoLinkGmailRecord(record: Record<string, any>): boolean {
  if (localGmailAccountsFromRecord(record).length > 0) return false;
  if (record.linked === true && record.skipped !== true) return false;
  if (record.skipped !== true) return true;
  const notes = String(record.notes || "").toLowerCase();
  return notes.includes("bootstrap") || notes.includes("local search pipeline");
}

function discoverMsgvaultAccounts(dbPath: string): { accounts: string[]; rows: Record<string, any>[]; error?: string } {
  if (!dbPath || !fs.existsSync(dbPath)) return { accounts: [], rows: [], error: dbPath ? "msgvault database not found" : "msgvault database path is empty" };
  try {
    const result = spawnSync("uv", [
      "run", "--project", ".", "python",
      "packs/ingestion/primitives/gmail_network_import/gmail_network_import.py",
      "msgvault-accounts",
      "--db", dbPath,
    ], {
      cwd: powerpacksRepoRoot,
      env: setupProcessEnv(),
      encoding: "utf8",
      timeout: 15000,
    });
    const payload = parseJsonFragment(result.stdout || "");
    const rows = Array.isArray(payload?.accounts) ? payload.accounts as Record<string, any>[] : [];
    const accounts = uniqueStrings(rows.map((row) => row.account_email));
    const error = result.status === 0 ? "" : (result.stderr || result.error?.message || "msgvault account discovery failed");
    return { accounts, rows, error: error || undefined };
  } catch (err) {
    return { accounts: [], rows: [], error: err instanceof Error ? err.message : String(err) };
  }
}

function autoLinkGmailFromMsgvault(accounts: RunState | null): RunState {
  const records = accountRecords(accounts);
  const gmail = records.gmail || {};
  if (!shouldAutoLinkGmailRecord(gmail)) return accounts || {};
  const dbPath = configuredMsgvaultDb(accounts);
  const discovered = discoverMsgvaultAccounts(dbPath);
  if (discovered.accounts.length === 0) return accounts || {};

  const now = new Date().toISOString();
  const config = gmail.config && typeof gmail.config === "object" ? gmail.config : {};
  const next: RunState = accounts && typeof accounts === "object" ? { ...accounts } : { version: 2 };
  const nextRecords = { ...records };
  nextRecords.gmail = {
    ...gmail,
    linked: true,
    skipped: false,
    usernames: discovered.accounts,
    artifacts: Array.isArray(gmail.artifacts) ? gmail.artifacts : [],
    config: {
      ...config,
      msgvault_db: dbPath,
      account_emails: uniqueStrings([...(Array.isArray(config.account_emails) ? config.account_emails : []), ...discovered.accounts]),
      available_accounts: discovered.accounts,
      selected_accounts: discovered.accounts,
      pending_accounts: [],
    },
    last_checked_at: now,
    last_success_at: now,
    notes: "Auto-linked Gmail accounts already present in local msgvault; no Gmail sync or import was run.",
  };
  next.accounts = nextRecords;
  next.updated_at = now;
  try {
    writeJsonSync(accountsPath, next);
  } catch {
    return next;
  }
  return next;
}

function imessagePermissionStatus(): Record<string, any> {
  const base = {
    status: "permission_required",
    chat_db: messagesChatDbPath,
    exists: false,
    readable: false,
    permission_required: true,
    message: "Full Disk Access is required to read Messages chat.db.",
  };
  if (!messagesChatDbPath) {
    return { ...base, error: "HOME is not set, so Messages chat.db could not be located." };
  }
  if (!fs.existsSync(messagesChatDbPath)) {
    return { ...base, error: "Messages chat.db was not found." };
  }
  try {
    const fd = fs.openSync(messagesChatDbPath, "r");
    fs.closeSync(fd);
    return {
      ...base,
      status: "ready",
      exists: true,
      readable: true,
      permission_required: false,
      message: "Messages chat.db is readable.",
    };
  } catch (err) {
    return {
      ...base,
      exists: true,
      error: err instanceof Error ? err.message : String(err),
    };
  }
}

function whatsappLinkStatus(): Record<string, any> {
  const nowMs = Date.now();
  if (cachedWhatsAppLinkStatus && cachedWhatsAppLinkStatus.expiresAt > nowMs) {
    return cachedWhatsAppLinkStatus.value;
  }

  const base = {
    status: "not_authenticated",
    authenticated: false,
    store: whatsAppStorePath,
  };
  let value: Record<string, any> = base;
  try {
    const result = spawnSync("uv", [
      "run", "--project", ".", "python",
      "packs/messages/primitives/import_whatsapp_wacli/import_whatsapp_wacli.py",
      "status",
      "--store", whatsAppStorePath,
    ], {
      cwd: powerpacksRepoRoot,
      env: setupProcessEnv(),
      encoding: "utf8",
      timeout: 8000,
    });
    const payload = parseJsonFragment(result.stdout || "");
    const auth = payload?.auth && typeof payload.auth === "object" ? payload.auth : {};
    const authenticated = auth.authenticated === true;
    value = {
      ...base,
      status: authenticated ? "authenticated" : "not_authenticated",
      authenticated,
    };
    if (!authenticated) {
      const error = payload?.error || auth.error || result.error?.message || result.stderr;
      if (error) value.error = String(error).slice(0, 500);
    }
  } catch (err) {
    value = {
      ...base,
      error: err instanceof Error ? err.message : String(err),
    };
  }
  cachedWhatsAppLinkStatus = { expiresAt: nowMs + 5000, value };
  return value;
}

function messagesLinkStatus(): Record<string, Record<string, any>> {
  return {
    imessage: imessagePermissionStatus(),
    whatsapp: whatsappLinkStatus(),
  };
}

function normalizeSetupSources(accounts: RunState | null) {
  const records = accountRecords(accounts);
  const messagesStatus = messagesLinkStatus();
  return SETUP_SOURCE_ORDER.map((id) => {
    const record = records[id] || {};
    const linked = Boolean(record.linked || record.status === "linked");
    const skipped = Boolean(record.skipped || record.status === "skipped");
    const baseConfig = record.config && typeof record.config === "object" ? record.config : {};
    const config = id === "messages"
      ? {
        ...baseConfig,
        imessage: {
          ...((baseConfig.imessage && typeof baseConfig.imessage === "object") ? baseConfig.imessage : {}),
          ...messagesStatus.imessage,
        },
        whatsapp: {
          ...((baseConfig.whatsapp && typeof baseConfig.whatsapp === "object") ? baseConfig.whatsapp : {}),
          ...messagesStatus.whatsapp,
        },
      }
      : baseConfig;
    const liveStatus = id === "messages" && !skipped
      ? (messagesStatus.imessage.status === "ready" ? (linked ? "linked" : "ready") : "permission_required")
      : null;
    return {
      id,
      label: SETUP_SOURCE_LABELS[id],
      status: skipped ? "skipped" : liveStatus || (linked ? "linked" : record.status || "unlinked"),
      linked,
      skipped,
      usernames: Array.isArray(record.usernames) ? record.usernames.map(String) : [],
      artifacts: Array.isArray(record.artifacts) ? record.artifacts.map(String) : [],
      notes: record.notes || "",
      lastCheckedAt: record.last_checked_at || record.lastCheckedAt || null,
      lastSuccessAt: record.last_success_at || record.lastSuccessAt || null,
      config,
    };
  });
}

function sourceSlug(value: string): string {
  return (value || "source").toLowerCase().replace(/[^a-z0-9._-]+/g, "-").replace(/^[._-]+|[._-]+$/g, "") || "source";
}

function ledgerStatus(filePath: string, fallback: string) {
  const ledger = readJsonSync(filePath) || {};
  const block = ledger.current_block && typeof ledger.current_block === "object" ? ledger.current_block : null;
  const summary = fileSummary(filePath);
  return {
    status: String(block?.status || ledger.status || fallback),
    updatedAt: ledger.updated_at || ledger.completed_at || summary.updatedAt || null,
    runId: ledger.run_id || "",
  };
}

function messagesLedgerStatus(fallback: string) {
  const ledger = readJsonSync(messagesLedgerPath) || {};
  const summary = fileSummary(messagesLedgerPath);
  let reviewCounts: Record<string, any> = {};
  try {
    reviewCounts = messagesReviewResponse("all", "", 0, 1).counts || {};
  } catch {
    reviewCounts = {};
  }
  const block = messagesCurrentBlockForUi(ledger, reviewCounts);
  const rawStatus = String(block?.status || ledger.status || fallback);
  return {
    status: rawStatus === "selected_steps_completed" ? "completed" : rawStatus,
    updatedAt: ledger.updated_at || ledger.completed_at || summary.updatedAt || null,
    runId: ledger.run_id || "",
  };
}

function importNetworkCommand(operatorId: string, extra: string[] = []) {
  return [
    "uv", "run", "--project", ".", "python",
    "packs/ingestion/primitives/import_network_pipeline/import_network_pipeline.py",
    "run",
    "--from-accounts", ".powerpacks/ingestion/accounts.json",
    "--operator-id", operatorId,
    "--include-existing-artifacts",
    "--ledger", ".powerpacks/network-import/import-network-run.setup-refresh.json",
    ...extra,
  ];
}

function messageImportCommand(source: ReturnType<typeof normalizeSetupSources>[number]) {
  const whatsapp = source.config.whatsapp && typeof source.config.whatsapp === "object" ? source.config.whatsapp as Record<string, any> : {};
  const imessage = source.config.imessage && typeof source.config.imessage === "object" ? source.config.imessage as Record<string, any> : {};
  const includeFlags = [];
  if (imessage.status !== "skipped") includeFlags.push("--include-imessage");
  if (whatsapp.status === "linked" || whatsapp.authenticated === true) includeFlags.push("--include-whatsapp");
  includeFlags.push(
    "--include-contact-merge",
    "--include-powerset-candidates",
    "--include-local-match",
    "--include-llm-review"
  );
  const refreshFlags = [];
  if (includeFlags.includes("--include-imessage")) refreshFlags.push("--force-imessage");
  if (includeFlags.includes("--include-whatsapp")) refreshFlags.push("--force-whatsapp");
  refreshFlags.push(
    "--force-sync-candidates",
    "--force-match",
    "--rerun-llm",
    "--force-build-review"
  );
  return [
    "uv", "run", "--project", ".", "python",
    "packs/messages/primitives/import_contacts_pipeline/import_contacts_pipeline.py",
    "run",
    "--ledger", ".powerpacks/messages/import-run.setup-messages.json",
    "--parallel-timeout", String(process.env.POWERPACKS_SETUP_MESSAGES_PARALLEL_TIMEOUT_SECONDS || "900"),
    "--reuse-existing-artifacts",
    ...includeFlags,
    ...refreshFlags,
  ];
}

function buildImportSources(accounts: RunState | null, operatorId: string): SetupImportSource[] {
  const sources = normalizeSetupSources(accounts);
  const byId = Object.fromEntries(sources.map((source) => [source.id, source]));
  const rows: SetupImportSource[] = [];
  const setupRefreshLedger = ".powerpacks/network-import/import-network-run.setup-refresh.json";
  const refreshState = ledgerStatus(path.join(powerpacksRepoRoot, setupRefreshLedger), "ready");

  const gmail = byId.gmail;
  const gmailAccounts = [
    ...new Set([
      ...((Array.isArray(gmail?.config?.selected_accounts) ? gmail.config.selected_accounts : []) as unknown[]).map(String),
      ...((Array.isArray(gmail?.config?.account_emails) ? gmail.config.account_emails : []) as unknown[]).map(String),
      ...((gmail?.usernames || []) as string[]),
    ].filter(Boolean)),
  ];
  rows.push({
    id: "gmail",
    sourceId: "gmail",
    label: "Gmail",
    status: gmail?.skipped ? "skipped" : gmail?.linked ? refreshState.status : "not_linked",
    linked: Boolean(gmail?.linked),
    skipped: Boolean(gmail?.skipped),
    accountEmail: gmailAccounts.length === 1 ? gmailAccounts[0] : undefined,
    accountCount: gmailAccounts.length,
    updatedAt: gmail?.linked && !gmail?.skipped ? refreshState.updatedAt : null,
    runId: gmail?.linked && !gmail?.skipped ? refreshState.runId : "",
    command: importNetworkCommand(operatorId, ["--only-source", "gmail", "--force"]),
  });

  for (const id of ["linkedin_csv", "messages", "twitter"] as const) {
    const source = byId[id];
    const ledger = id === "messages" ? ".powerpacks/messages/import-run.setup-messages.json" : setupRefreshLedger;
    const state = id === "messages"
      ? messagesLedgerStatus(source?.linked ? "ready" : source?.skipped ? "skipped" : "not_linked")
      : refreshState;
    rows.push({
      id,
      sourceId: id,
      label: SETUP_SOURCE_LABELS[id],
      status: source?.skipped ? "skipped" : source?.linked ? state.status : "not_linked",
      linked: Boolean(source?.linked),
      skipped: Boolean(source?.skipped),
      updatedAt: source?.linked && !source?.skipped ? state.updatedAt : null,
      runId: source?.linked && !source?.skipped ? state.runId : "",
      runnable: id === "twitter" ? false : undefined,
      disabledReason: id === "twitter" ? "Twitter/X handle is recorded; follower import is not wired into setup yet." : undefined,
      command: id === "messages"
        ? messageImportCommand(source)
        : id === "twitter"
          ? []
          : importNetworkCommand(operatorId, ["--only-source", id, "--force"]),
    });
  }

  return rows;
}

function messageChannelFlags(accounts: RunState | null, ledger: RunState | null): string[] {
  const sources = normalizeSetupSources(accounts);
  const messages = sources.find((source) => source.id === "messages");
  const config = messages?.config || {};
  const imessage = config.imessage && typeof config.imessage === "object" ? config.imessage as Record<string, any> : {};
  const whatsapp = config.whatsapp && typeof config.whatsapp === "object" ? config.whatsapp as Record<string, any> : {};
  const steps = ledger?.steps || {};
  const flags: string[] = [];
  if (steps.extract_imessage?.status === "completed" || imessage.status === "ready" || imessage.readable === true) {
    flags.push("--include-imessage");
  }
  if (steps.extract_whatsapp?.status === "completed" || whatsapp.status === "linked" || whatsapp.status === "authenticated" || whatsapp.authenticated === true) {
    flags.push("--include-whatsapp");
  }
  return flags;
}

function messagesCompleteReviewCommand(accounts: RunState | null): string[] {
  const ledger = readJsonSync(messagesLedgerPath) || {};
  const markAppReviewComplete = [
    "uv", "run", "--project", ".", "python", "-c",
    [
      "import datetime, json",
      "from pathlib import Path",
      "p = Path('.powerpacks/messages/import-run.setup-messages.json')",
      "now = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')",
      "data = json.loads(p.read_text(encoding='utf-8')) if p.exists() else {}",
      "step = data.setdefault('steps', {}).setdefault('review_research_web', {'id': 'review_research_web'})",
      "step.update({'status': 'completed', 'finished_at': now, 'summary': {'source': 'powerpacks_setup_app', 'url': '/setup/imessage/review'}})",
      "data.pop('current_block', None)",
      "data['updated_at'] = now",
      "p.parent.mkdir(parents=True, exist_ok=True)",
      "p.write_text(json.dumps(data, indent=2, sort_keys=True) + '\\n', encoding='utf-8')",
    ].join("; "),
  ];
  const continueAfterReview = [
    "uv", "run", "--project", ".", "python",
    "packs/messages/primitives/import_contacts_pipeline/import_contacts_pipeline.py",
    "continue",
    "--ledger", ".powerpacks/messages/import-run.setup-messages.json",
    "--parallel-timeout", String(process.env.POWERPACKS_SETUP_MESSAGES_PARALLEL_TIMEOUT_SECONDS || "900"),
    "--reuse-existing-artifacts",
    ...messageChannelFlags(accounts, ledger),
    "--include-contact-merge",
    "--include-powerset-candidates",
    "--include-local-match",
    "--include-llm-review",
    "--include-research",
    "--include-review",
    "--force-prepare-queue",
  ];
  return ["/bin/zsh", "-lc", `${shellJoin(markAppReviewComplete)} && ${shellJoin(continueAfterReview)}`];
}

function messagesApproveAndContinueCommand(accounts: RunState | null): string[] {
  const approve = [
    "uv", "run", "--project", ".", "python",
    "packs/messages/primitives/import_contacts_pipeline/import_contacts_pipeline.py",
    "approve",
    "--ledger", ".powerpacks/messages/import-run.setup-messages.json",
    "--confirm",
  ];
  return ["/bin/zsh", "-lc", `${shellJoin(approve)} && ${shellJoin(messagesCompleteReviewCommand(accounts))}`];
}

function csvPathCount(value: unknown): number {
  const paths = new Set<string>();
  const visit = (item: unknown) => {
    if (Array.isArray(item)) {
      item.forEach(visit);
      return;
    }
    if (typeof item !== "string" || !item.trim()) return;
    paths.add(item.trim());
  };
  visit(value);
  let count = 0;
  for (const item of paths) {
    const resolved = safeJoinPowerpacks(item);
    if (!resolved || !fs.existsSync(resolved)) continue;
    try {
      count += parseCsvDocument(fs.readFileSync(resolved, "utf8")).rows.length;
    } catch {
      // Ignore malformed or currently written artifacts in status summaries.
    }
  }
  return count;
}

function firstCsvCount(...values: unknown[]): number {
  for (const value of values) {
    const count = csvPathCount(value);
    if (count > 0) return count;
  }
  return 0;
}

function sha256File(filePath: string): string {
  try {
    return createHash("sha256").update(fs.readFileSync(filePath)).digest("hex");
  } catch {
    return "";
  }
}

function localDuckdbTableCounts(duckdbPath: string): Array<{ name: string; rows: number }> {
  if (!duckdbPath || !fs.existsSync(duckdbPath)) return [];
  const stat = fs.statSync(duckdbPath);
  const key = `${duckdbPath}:${stat.mtimeMs}:${stat.size}`;
  const nowMs = Date.now();
  if (cachedDuckdbTables && cachedDuckdbTables.key === key && cachedDuckdbTables.expiresAt > nowMs) {
    return cachedDuckdbTables.value;
  }
  const script = `
import duckdb, json
tables = ["local_people", "local_people_positions", "local_summaries", "local_people_education", "local_education", "local_companies"]
out = []
con = duckdb.connect(${JSON.stringify(duckdbPath)}, read_only=True)
for table in tables:
    try:
        out.append({"name": table, "rows": int(con.execute(f"select count(*) from {table}").fetchone()[0])})
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
        .map((row) => ({ name: String(row.name || ""), rows: Number(row.rows || 0) }))
        .filter((row) => row.name);
    }
  } catch {
    value = [];
  }
  cachedDuckdbTables = { key, expiresAt: nowMs + 10000, value };
  return value;
}

function indexDryRunEstimate(operatorId: string, peopleSha256: string): Record<string, any> {
  const peopleCsv = path.join(powerpacksStateRoot, "network-import", "merged", "people.csv");
  if (!fs.existsSync(peopleCsv)) return {};
  const key = `${operatorId}:${peopleSha256 || sha256File(peopleCsv)}`;
  const nowMs = Date.now();
  if (cachedIndexEstimate && cachedIndexEstimate.key === key && cachedIndexEstimate.expiresAt > nowMs) {
    return cachedIndexEstimate.value;
  }
  const result = spawnSync("uv", [
    "run", "--project", ".", "python",
    "packs/indexing/primitives/build_processing_pipeline/build_processing_pipeline.py",
    "run",
    "--dry-run",
    "--input", ".powerpacks/network-import/merged/people.csv",
    "--output-dir", ".powerpacks/search-index",
    "--default-operator-id", operatorId,
  ], {
    cwd: powerpacksRepoRoot,
    env: setupProcessEnv(),
    encoding: "utf8",
    timeout: 45000,
  });
  const payload = parseJsonFragment(result.stdout || "") || {};
  const value = {
    status: payload.status || (result.status === 0 ? "dry_run" : "failed"),
    totalEstimatedUsd: Number(payload.total_estimated_usd ?? payload.estimated_cost_usd ?? 0),
    estimatedPaidCalls: payload.estimated_paid_calls || {},
    counts: payload.counts || {},
    providers: payload.providers || {},
    error: result.status === 0 ? "" : String(payload.error || result.stderr || result.error?.message || "").slice(0, 1000),
  };
  cachedIndexEstimate = { key, expiresAt: nowMs + 60000, value };
  return value;
}

function readRelativeLedger(relativePath: string): RunState {
  return readJsonSync(path.join(powerpacksRepoRoot, relativePath)) || {};
}

function buildEnrichmentSources(setupSources: ReturnType<typeof normalizeSetupSources> = []): SetupEnrichmentSource[] {
  const setupRefreshLedger = readRelativeLedger(".powerpacks/network-import/import-network-run.setup-refresh.json");
  const refreshArtifacts = setupRefreshLedger.artifacts || {};
  const linkedInStep = setupRefreshLedger.steps?.linkedin || {};
  const sourceById = Object.fromEntries(setupSources.map((source) => [source.id, source]));
  const isSkipped = (id: string) => Boolean(sourceById[id]?.skipped);

  const messagesLedger = readJsonSync(messagesLedgerPath) || {};
  const messagesReview = messagesLedger.steps?.llm_review?.summary || {};
  const messagesCounts = messagesReview.counts || {};
  const messagesMatchStats = messagesLedger.steps?.match_local_contacts?.summary?.stats || {};

  const sumArtifacts = (artifactsList: Record<string, any>[], key: string) =>
    artifactsList.reduce((total, artifacts) => total + csvPathCount(artifacts[key]), 0);

  return [
    {
      id: "linkedin_csv",
      label: "LinkedIn",
      status: String(linkedInStep.status || setupRefreshLedger.status || "unknown"),
      candidates: firstCsvCount(refreshArtifacts.linkedin_linkedin_enrichment_queue_csv, refreshArtifacts.linkedin_source_people_csv),
      enriched: firstCsvCount(refreshArtifacts.linkedin_people_csv, refreshArtifacts.linkedin_enrich_people_people_csv, refreshArtifacts.linkedin_provider_enriched_csv),
      skipped: firstCsvCount(refreshArtifacts.linkedin_skipped_enrichment_csv, refreshArtifacts.linkedin_enrich_people_skipped_enrichment_csv),
      matched: csvPathCount(refreshArtifacts.linkedin_rapidapi_cache_hits_csv) + csvPathCount(refreshArtifacts.linkedin_provider_enriched_csv),
      updatedAt: isSkipped("linkedin_csv") ? null : linkedInStep.finished_at || setupRefreshLedger.updated_at || null,
    },
    {
      id: "gmail",
      label: "Gmail",
      status: String(setupRefreshLedger.status || "unknown"),
      candidates: sumArtifacts([refreshArtifacts], "gmail_people_csvs") || sumArtifacts([refreshArtifacts], "gmail_people_csv"),
      enriched: sumArtifacts([refreshArtifacts], "gmail_final_people_csvs"),
      skipped: 0,
      matched: sumArtifacts([refreshArtifacts], "gmail_resolved_people_csvs"),
      updatedAt: isSkipped("gmail") ? null : setupRefreshLedger.updated_at || setupRefreshLedger.steps?.source_imports?.finished_at || null,
    },
    {
      id: "messages",
      label: "Messages",
      status: String(messagesReview.status || messagesLedger.status || "unknown"),
      candidates: Number(messagesCounts.enrich || messagesReview.candidate_count || messagesCounts.verdicts || 0) || 0,
      enriched: Number(messagesMatchStats.matched || 0) || 0,
      skipped: Number(messagesCounts.skip || 0) || 0,
      matched: Number(messagesMatchStats.matched || 0) || 0,
      updatedAt: isSkipped("messages") ? null : messagesLedger.updated_at || messagesReview.started_at || null,
    },
    {
      id: "twitter",
      label: "Twitter/X",
      status: isSkipped("twitter") ? "skipped" : String(setupRefreshLedger.steps?.twitter?.status || "unknown"),
      candidates: firstCsvCount(refreshArtifacts.twitter_people_csv, refreshArtifacts.people_csv),
      enriched: firstCsvCount(refreshArtifacts.twitter_people_csv, refreshArtifacts.people_csv),
      skipped: 0,
      matched: 0,
      updatedAt: isSkipped("twitter") ? null : setupRefreshLedger.steps?.twitter?.finished_at || setupRefreshLedger.updated_at || null,
    },
  ];
}

function publicImportSources(sources: SetupImportSource[]) {
  return sources.map(({ command: _command, ...source }) => source);
}

function deriveNextAction(setupLedger: RunState | null, sources: ReturnType<typeof normalizeSetupSources>) {
  const phases = setupLedger?.phases || {};
  if ((phases.bootstrap?.status || "pending") === "pending") {
    const bundles = fs.existsSync(path.join(powerpacksStateRoot, "operator-bootstrap", "bundles"))
      ? fs.readdirSync(path.join(powerpacksStateRoot, "operator-bootstrap", "bundles")).filter((file) => file.endsWith(".operator-bootstrap.tar.gz"))
      : [];
    if (bundles.length > 0) return { status: "run_command", phase: "bootstrap", reason: "bootstrap bundle available" };
  }
  if (phases.import?.status === "refresh_due") return { status: "run_command", phase: "import", reason: phases.import?.refresh_due?.reason || "refresh due" };
  if (["needs_processing", "not_ready"].includes(phases.index?.status)) return { status: "run_command", phase: "index", reason: phases.index?.reason || "index not ready" };
  return { status: "done", phase: "ready", reason: setupLedger?.status || "ready" };
}

async function setupStatus() {
  const setupLedger = readJsonSync(setupLedgerPath) || {};
  const accounts = autoLinkGmailFromMsgvault(readJsonSync(accountsPath) || {});
  const importRefreshLedger = readJsonSync(importRefreshLedgerPath) || {};
  const messagesLedger = readJsonSync(messagesLedgerPath) || {};
  const reviewPath = resolveReviewCsvPath() || messagesReviewCsvPath;
  const reviewApi = messagesReviewResponse("all", "", 0, 1);
  const phases = setupLedger.phases || {};
  const sources = normalizeSetupSources(accounts);
  const linkedSources = sources.filter((source) => source.linked).map((source) => source.id);
  const skippedSources = sources.filter((source) => source.skipped).map((source) => source.id);
  const unresolvedSources = sources.filter((source) => !source.linked && !source.skipped).map((source) => source.id);
  const operator = resolveOperator(setupLedger, accounts);
  const importSources = buildImportSources(accounts, operator.id);
  const enrichmentSources = buildEnrichmentSources(sources);
  const bootstrap = bootstrapSummary(operator);
  const setupFile = fileSummary(setupLedgerPath);
  const importFile = fileSummary(importRefreshLedgerPath);
  const indexPhase = phases.index || {};
  const peopleCsvPath = path.join(powerpacksStateRoot, "network-import", "merged", "people.csv");
  const duckdbPath = path.join(powerpacksStateRoot, "search-index", "local-search.duckdb");
  const peopleSha256 = String(indexPhase.people_sha256 || sha256File(peopleCsvPath) || "");
  const duckdbFile = fileSummary(duckdbPath);
  const duckdbTables = localDuckdbTableCounts(duckdbPath);
  const duckdbHasRows = duckdbTables.some((table) => Number(table.rows || 0) > 0);
  const bootstrapRestorePreferred = Number(bootstrap.peopleRecords || 0) > 0
    && (!duckdbFile.exists || !duckdbHasRows);
  const processingEstimate = bootstrapRestorePreferred ? {
    status: "local_records_restore",
    totalEstimatedUsd: 0,
    estimatedPaidCalls: {},
    counts: { people: Number(bootstrap.peopleRecords || 0) },
    providers: {},
    error: "",
  } : indexDryRunEstimate(operator.id, peopleSha256);
  const importLiveRefresh = phases.import?.live_refresh || importRefreshLedger.refresh || importRefreshLedger;
  const messagesCurrentBlock = messagesCurrentBlockForUi(messagesLedger, reviewApi.counts || {});

  return {
    operator,
    bootstrap,
    setup: {
      ...setupFile,
      status: setupLedger.status || "unknown",
      updatedAt: setupLedger.updated_at || setupFile.updatedAt || null,
      phases: {
        bootstrap: phases.bootstrap?.status || "unknown",
        link: phases.link?.status || "unknown",
        import: phases.import?.status || "unknown",
        index: phases.index?.status || "unknown",
      },
    },
    accounts: {
      ...fileSummary(accountsPath),
      operatorId: accounts.operator_id || accounts.operatorId || null,
      linkedSources,
      skippedSources,
      unresolvedSources,
      sources,
    },
    messages: {
      ...fileSummary(messagesLedgerPath),
      status: messagesLedger.status || "unknown",
      currentBlock: messagesCurrentBlock,
      steps: Object.fromEntries(Object.entries(messagesLedger.steps || {}).map(([key, value]: [string, any]) => [key, value?.status || "unknown"])),
    },
    review: {
      ...fileSummary(reviewPath),
      counts: reviewApi.counts,
    },
    import: {
      ...importFile,
      status: importLiveRefresh?.status || phases.import?.status || "unknown",
      updatedAt: importLiveRefresh?.completed_at || importLiveRefresh?.updated_at || importFile.updatedAt || null,
      runId: importLiveRefresh?.run_id || importRefreshLedger.run_id || "",
      linkedSources: Array.isArray(importLiveRefresh?.linked_sources) ? importLiveRefresh.linked_sources : [],
      gmailSyncAfter: importLiveRefresh?.gmail_sync_after || "",
      sources: publicImportSources(importSources),
    },
    enrichment: {
      status: summarizeEnrichmentStatus(enrichmentSources),
      totalCandidates: enrichmentSources.reduce((total, source) => total + source.candidates, 0),
      totalEnriched: enrichmentSources.reduce((total, source) => total + source.enriched, 0),
      sources: enrichmentSources,
    },
    index: {
      duckdb: indexPhase.duckdb || ".powerpacks/search-index/local-search.duckdb",
      duckdbExists: duckdbFile.exists,
      duckdbUpdatedAt: duckdbFile.updatedAt || null,
      duckdbSizeBytes: duckdbFile.sizeBytes || 0,
      duckdbTables,
      peopleCsv: indexPhase.people_csv || ".powerpacks/network-import/merged/people.csv",
      peopleRecords: csvPathCount(".powerpacks/network-import/merged/people.csv"),
      peopleSha256,
      readiness: indexPhase.status || "unknown",
      reason: indexPhase.reason || "",
      indexInputSha256: indexPhase.index_input_sha256 || "",
      processingEstimate,
    },
    next: deriveNextAction(setupLedger, sources),
    jobs: setupJobsList(),
  };
}

function requireString(value: unknown, label: string): string {
  const text = String(value || "").trim();
  if (!text) throw new Error(`${label} is required`);
  return text;
}

function requireSource(value: unknown): string {
  const source = requireString(value, "source");
  if (!SETUP_SOURCE_ORDER.includes(source as any)) throw new Error(`unsupported source: ${source}`);
  return source;
}

function saveLinkedInCsvUpload(body: Record<string, any>) {
  const filename = sourceSlug(path.basename(String(body.filename || "Connections.csv")) || "Connections.csv");
  if (!filename.toLowerCase().endsWith(".csv")) throw new Error("LinkedIn export must be a CSV file");
  const content = String(body.content || "");
  if (!content.trim()) throw new Error("LinkedIn CSV is empty");
  const bytes = Buffer.byteLength(content, "utf8");
  if (bytes > 25 * 1024 * 1024) throw new Error("LinkedIn CSV is too large");
  const outputDir = path.join(powerpacksStateRoot, "ingestion", "uploads", "linkedin");
  fs.mkdirSync(outputDir, { recursive: true });
  const stamp = new Date().toISOString().replace(/[-:]/g, "").replace(/\.\d+Z$/, "Z");
  const output = path.join(outputDir, `${stamp}-${filename}`);
  fs.writeFileSync(output, content, "utf8");
  return {
    status: "ok",
    path: output,
    sizeBytes: bytes,
  };
}

function buildSetupActionJob(body: Record<string, any>): SetupJob {
  const setupLedger = readJsonSync(setupLedgerPath) || {};
  const accounts = autoLinkGmailFromMsgvault(readJsonSync(accountsPath) || {});
  const operator = resolveOperator(setupLedger, accounts);
  const action = requireString(body.action, "action");

  if (action === "run-command") {
    return startWhitelistedShellJob(requireString(body.command, "command"));
  }

  if (action === "import") {
    return startSetupJob(action, setupCommandArgs(operator.id, "import"), 6 * 60 * 60 * 1000);
  }

  if (action === "index") {
    const extra = body.approveProviderSpend === true ? ["--approve-provider-spend"] : [];
    return startSetupJob(action, setupCommandArgs(operator.id, "index", extra));
  }

  if (["bootstrap", "link", "run"].includes(action)) {
    return startSetupJob(action, setupCommandArgs(operator.id, action as any));
  }

  if (action === "enrich-all") {
    return startSetupJob(action, setupCommandArgs(operator.id, "import"), 6 * 60 * 60 * 1000);
  }

  if (action === "import-source") {
    const sourceId = requireString(body.source, "source");
    const source = buildImportSources(accounts, operator.id).find((candidate) => candidate.id === sourceId);
    if (!source) throw new Error(`unsupported import source: ${sourceId}`);
    if (!source.linked) throw new Error(`source is not linked: ${sourceId}`);
    if (source.skipped) throw new Error(`source is skipped: ${sourceId}`);
    if (source.runnable === false || source.command.length === 0) {
      throw new Error(source.disabledReason || `source is not importable yet: ${sourceId}`);
    }
    const fanIn = setupCommandArgs(operator.id, "fan-in", ["--force"]);
    return startSetupJob(action, importAndFanInCommand(source.command, fanIn, `importing ${source.label}`), 6 * 60 * 60 * 1000);
  }

  if (action === "enrich-source") {
    const sourceId = requireString(body.source, "source");
    const source = buildImportSources(accounts, operator.id).find((candidate) => candidate.id === sourceId);
    if (!source) throw new Error(`unsupported import source: ${sourceId}`);
    if (!source.linked) throw new Error(`source is not linked: ${sourceId}`);
    if (source.skipped) throw new Error(`source is skipped: ${sourceId}`);
    if (source.runnable === false || source.command.length === 0) {
      throw new Error(source.disabledReason || `source is not importable yet: ${sourceId}`);
    }
    const fanIn = setupCommandArgs(operator.id, "fan-in", ["--force"]);
    return startSetupJob(action, importAndFanInCommand(source.command, fanIn, `enriching ${source.label}`), 6 * 60 * 60 * 1000);
  }

  if (action === "skip-source") {
    return startSetupJob(action, setupCommandArgs(operator.id, "link", ["--skip-source", requireSource(body.source)]));
  }

  if (action === "gmail-add-email") {
    return startSetupJob(action, setupCommandArgs(operator.id, "link", ["--gmail-add-email", requireString(body.email, "email")]));
  }

  if (action === "gmail-authorized-email") {
    return startSetupJob(action, setupCommandArgs(operator.id, "link", ["--gmail-authorized-email", requireString(body.email, "email")]));
  }

  if (action === "gmail-account") {
    return startSetupJob(action, setupCommandArgs(operator.id, "link", ["--gmail-account", requireString(body.email, "email")]));
  }

  if (action === "gmail-all") {
    return startSetupJob(action, setupCommandArgs(operator.id, "link", ["--gmail-all"]));
  }

  if (action === "gmail-link-emails") {
    return startSetupJob(action, gmailLinkCommand(operator.id, body.emails), 60 * 60 * 1000);
  }

  if (action === "linkedin-csv") {
    return startSetupJob(action, setupCommandArgs(operator.id, "link", [
      "--linkedin-csv", requireString(body.csvPath, "csvPath"),
      "--linkedin-source-user", requireString(body.sourceLabel, "sourceLabel"),
    ]));
  }

  if (action === "messages-link") {
    const extra: string[] = ["--messages-check"];
    if (body.skipWhatsapp) extra.push("--skip-messages-whatsapp");
    return startSetupJob(action, setupCommandArgs(operator.id, "link", extra), 10 * 60 * 1000);
  }

  if (action === "messages-continue") {
    return startSetupJob(action, [
      "uv", "run", "--project", ".", "python",
      "packs/messages/primitives/import_contacts_pipeline/import_contacts_pipeline.py",
      "continue",
      "--ledger", ".powerpacks/messages/import-run.setup-messages.json",
      "--reuse-existing-artifacts",
      "--include-imessage",
      "--include-whatsapp",
      "--include-contact-merge",
      "--include-powerset-candidates",
      "--include-local-match",
      "--include-llm-review",
    ], 30 * 60 * 1000);
  }

  if (action === "messages-complete-review") {
    return startSetupJob(action, messagesCompleteReviewCommand(accounts), 2 * 60 * 60 * 1000);
  }

  if (action === "messages-approve-continue") {
    return startSetupJob(action, messagesApproveAndContinueCommand(accounts), 2 * 60 * 60 * 1000);
  }

  if (action === "whatsapp-auth") {
    cachedWhatsAppLinkStatus = null;
    return startSetupJob(action, [
      "uv", "run", "--project", ".", "python",
      "packs/messages/primitives/import_whatsapp_wacli/import_whatsapp_wacli.py",
      "auth",
      "--store", ".powerpacks/messages/wacli",
    ], 10 * 60 * 1000);
  }

  if (action === "open-message-permissions") {
    return startSetupJob(action, [
      "uv", "run", "--project", ".", "python",
      "packs/messages/primitives/extract_imessage_contacts/extract_imessage_contacts.py",
      "open-privacy-settings",
      "--target", "both",
    ], 2 * 60 * 1000);
  }

  if (action === "twitter-handle") {
    return startSetupJob(action, setupCommandArgs(operator.id, "link", ["--twitter-handle", requireString(body.handle, "handle")]));
  }

  throw new Error(`unsupported setup action: ${action}`);
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

          if (url.pathname === "/local-api/setup/status") {
            return sendJson(res, await setupStatus());
          }

          if (url.pathname === "/local-api/setup/jobs") {
            return sendJson(res, { jobs: setupJobsList() });
          }

          if (url.pathname === "/local-api/setup/linkedin-csv-upload" && req.method === "POST") {
            return sendJson(res, saveLinkedInCsvUpload(await readRequestJson(req)));
          }

          const setupJobMatch = url.pathname.match(/^\/local-api\/setup\/jobs\/([^/]+)$/);
          if (setupJobMatch) {
            const job = setupJobs.get(decodeURIComponent(setupJobMatch[1]));
            return job ? sendJson(res, job) : sendJson(res, { error: "Setup job not found" }, 404);
          }

          if (url.pathname === "/local-api/setup/run" && req.method === "POST") {
            const body = await readRequestJson(req);
            const job = buildSetupActionJob(body);
            return sendJson(res, { job });
          }

          if (url.pathname === "/local-api/messages/review") {
            const offset = Math.max(0, Number(url.searchParams.get("offset") || 0) || 0);
            const limit = Math.min(500, Math.max(1, Number(url.searchParams.get("limit") || 100) || 100));
            const filter = (url.searchParams.get("filter") || "all").trim().toLowerCase();
            const query = url.searchParams.get("q") || "";
            return sendJson(res, messagesReviewResponse(filter, query, offset, limit));
          }

          if (url.pathname === "/local-api/messages/review/toggle" && req.method === "POST") {
            const body = await readRequestJson(req);
            return sendJson(res, messagesReviewPrimitive("toggle", [
              "--row", String(body.row ?? body.index ?? ""),
              "--selected", body.selected === true || String(body.selected).toLowerCase() === "true" ? "true" : "false",
            ]));
          }

          if (url.pathname === "/local-api/messages/review/hint" && req.method === "POST") {
            const body = await readRequestJson(req);
            return sendJson(res, messagesReviewPrimitive("hint", [
              "--row", String(body.row ?? body.index ?? ""),
              "--hint", String(body.hint || ""),
            ]));
          }

          if (url.pathname === "/local-api/messages/review/bulk-toggle" && req.method === "POST") {
            const body = await readRequestJson(req);
            return sendJson(res, messagesReviewPrimitive("bulk-toggle", [
              "--tab", String(body.tab || "in_network"),
              "--selected", body.selected === true || String(body.selected).toLowerCase() === "true" ? "true" : "false",
            ]));
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
