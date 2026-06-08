import type { Plugin } from "vite";
import path from "path";
import fs from "fs";
import fsp from "fs/promises";
import { spawn, spawnSync } from "child_process";
import { createHash, randomUUID } from "crypto";
import { createGunzip } from "zlib";
import { createInterface } from "readline";

const appRoot = path.resolve(__dirname, "..");
const powerpacksRepoRoot = path.resolve(
  appRoot,
  process.env.POWERPACKS_REPO_ROOT || ".."
);
const powerpacksStateRoot = path.join(powerpacksRepoRoot, ".powerpacks");
const discoverContactsSetupLedger = ".powerpacks/network-import/discover/ledger.setup.json";
const runsDir = path.join(powerpacksStateRoot, "runs");
const setupLedgerPath = path.join(powerpacksStateRoot, "setup", "setup-run.json");
const accountsPath = path.join(powerpacksStateRoot, "ingestion", "accounts.json");
const importRefreshLedgerPath = path.join(powerpacksRepoRoot, discoverContactsSetupLedger);
const messagesLedgerPath = path.join(powerpacksStateRoot, "messages", "import-run.setup-messages.json");
const messagesReviewCsvPath = path.join(powerpacksStateRoot, "messages", "research_review.csv");
const whatsAppWacliQrPngRelativePath = ".powerpacks/messages/wacli-login-qr.png";
const whatsAppWacliQrHtmlRelativePath = ".powerpacks/messages/wacli-login-qr.html";
const whatsAppWacliQrPngPath = path.join(powerpacksStateRoot, "messages", "wacli-login-qr.png");
const whatsAppWacliQrHtmlPath = path.join(powerpacksStateRoot, "messages", "wacli-login-qr.html");
const whatsAppWahaQrPngRelativePath = ".powerpacks/messages/whatsapp/qr.png";
const whatsAppWahaQrTxtRelativePath = ".powerpacks/messages/whatsapp/qr.txt";
const whatsAppWahaQrPngPath = path.join(powerpacksStateRoot, "messages", "whatsapp", "qr.png");
const whatsAppWahaQrTxtPath = path.join(powerpacksStateRoot, "messages", "whatsapp", "qr.txt");
const whatsAppWahaEngine = "NOWEB";
const whatsAppWahaImage = "devlikeapro/waha:noweb-2026.3.4";
const messagesChatDbPath = process.env.POWERPACKS_IMESSAGE_CHAT_DB
  ? path.resolve(process.env.POWERPACKS_IMESSAGE_CHAT_DB)
  : path.join(process.env.HOME || "", "Library", "Messages", "chat.db");
const whatsAppStorePath = ".powerpacks/messages/wacli";

type RunState = Record<string, any>;
type CsvDocument = { headers: string[]; rows: Record<string, string>[] };
type SetupJobStage = { label: string; index: number; total: number };
type SetupJob = {
  id: string;
  action: string;
  actionKey?: string;
  source?: string;
  stages?: SetupJobStage[];
  status: "running" | "completed" | "failed" | "blocked";
  startedAt: string;
  completedAt?: string | null;
  command: string[];
  code?: number | null;
  stdout?: string;
  stderr?: string;
  log?: string;
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
  artifactDir?: string;
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
  unresolved: number;
  estimatedCostUsd?: number | null;
  blocked?: boolean;
  updatedAt?: string | null;
};
type SetupOperator = { id: string; email?: string; label: string };
type SetupStatusTab = "link" | "discover" | "enrichment" | "index" | "all";
type EnvKeySpec = {
  key: string;
  label: string;
  provider: string;
  description: string;
  required: boolean;
  getUrl: string;
  docsUrl?: string;
  aliases?: string[];
};

const ENV_KEY_SPECS: EnvKeySpec[] = [
  {
    key: "OPENAI_API_KEY",
    label: "OpenAI API key",
    provider: "OpenAI",
    description: "Used for local search reranking, embeddings, and indexing enrichment.",
    required: true,
    getUrl: "https://platform.openai.com/api-keys",
  },
  {
    key: "RAPIDAPI_LINKEDIN_KEY",
    label: "RapidAPI LinkedIn key",
    provider: "RapidAPI",
    description: "Used for LinkedIn profile hydration during import and enrichment. RAPIDAPI_KEY is accepted as a legacy fallback.",
    required: true,
    getUrl: "https://rapidapi.com/pnd-team-pnd-team/api/professional-network-data",
    aliases: ["RAPIDAPI_KEY"],
  },
  {
    key: "PARALLEL_API_KEY",
    label: "Parallel API key",
    provider: "Parallel",
    description: "Used for Gmail and Messages LinkedIn resolution / deep research.",
    required: true,
    getUrl: "https://platform.parallel.ai/settings?tab=api-keys",
  },
  {
    key: "APOLLO_API_KEY",
    label: "Apollo API key",
    provider: "Apollo",
    description: "Used for Apollo-backed contact and enrichment workflows.",
    required: true,
    getUrl: "https://developer.apollo.io/keys#/keys",
  },
  {
    key: "TURBOPUFFER_API_KEY",
    label: "TurboPuffer API key",
    provider: "TurboPuffer",
    description: "Used by cloud-backed search primitives when local DuckDB is not selected.",
    required: false,
    getUrl: "https://turbopuffer.com",
  },
  {
    key: "DATABASE_URL",
    label: "Database URL",
    provider: "Postgres",
    description: "Used by cloud-backed search and operator bootstrap exports.",
    required: false,
    getUrl: "https://supabase.com/dashboard",
    aliases: ["SUPABASE_DATABASE_URL", "SUPABASE_DB_URL"],
  },
  {
    key: "RAPIDAPI_TWITTER_KEY",
    label: "RapidAPI Twitter/X key",
    provider: "RapidAPI",
    description: "Used only for Twitter/X import; RAPIDAPI_KEY is accepted as a legacy fallback.",
    required: false,
    getUrl: "https://rapidapi.com/hub",
    aliases: ["RAPIDAPI_KEY"],
  },
];

function summarizeEnrichmentStatus(sources: SetupEnrichmentSource[]): string {
  if (sources.some((source) => String(source.status).startsWith("blocked"))) return "blocked_user_action";
  if (sources.some((source) => source.status === "running")) return "running";
  if (sources.some((source) => source.enriched > 0)) return "completed";
  if (sources.some((source) => source.candidates > 0)) return "ready";
  return "unknown";
}

function normalizeSetupStatusTab(value: unknown): SetupStatusTab {
  const tab = String(value || "").trim().toLowerCase();
  if (tab === "import" || tab === "discover") return "discover";
  if (tab === "link" || tab === "enrichment" || tab === "index" || tab === "all") return tab;
  return "all";
}

function maskEnvValue(value: string): string {
  if (!value) return "";
  if (value.length <= 8) return "set";
  return `${value.slice(0, 4)}...${value.slice(-4)}`;
}

function envValueStatus(value: unknown): "present" | "empty" | "missing" {
  if (value === undefined) return "missing";
  return String(value || "").trim() ? "present" : "empty";
}

function envStatus() {
  const envPath = path.join(powerpacksRepoRoot, ".env");
  const env = readEnvSummary();
  const file = fileSummary(envPath);
  const keys = ENV_KEY_SPECS.map((spec) => {
    const primaryValue = env[spec.key];
    const primaryStatus = envValueStatus(primaryValue);
    const aliasMatches = (spec.aliases || []).map((alias) => ({
      key: alias,
      status: envValueStatus(env[alias]),
      valuePreview: envValueStatus(env[alias]) === "present" ? maskEnvValue(String(env[alias])) : "",
    }));
    const satisfiedAlias = aliasMatches.find((alias) => alias.status === "present");
    const satisfied = primaryStatus === "present" || Boolean(satisfiedAlias);
    const status = primaryStatus === "present"
      ? "present"
      : satisfiedAlias
        ? "present_via_alias"
        : primaryStatus;
    return {
      key: spec.key,
      label: spec.label,
      provider: spec.provider,
      description: spec.description,
      required: spec.required,
      getUrl: spec.getUrl,
      docsUrl: spec.docsUrl || "",
      aliases: aliasMatches,
      status,
      satisfied,
      satisfiedBy: primaryStatus === "present" ? spec.key : satisfiedAlias?.key || "",
      valuePreview: primaryStatus === "present" ? maskEnvValue(String(primaryValue)) : "",
    };
  });
  const required = keys.filter((key) => key.required);
  const missingRequired = required.filter((key) => !key.satisfied);
  return {
    ...file,
    keys,
    summary: {
      total: keys.length,
      required: required.length,
      ready: missingRequired.length === 0,
      missingRequired: missingRequired.length,
      present: keys.filter((key) => key.satisfied).length,
      empty: keys.filter((key) => key.status === "empty").length,
      missing: keys.filter((key) => key.status === "missing").length,
    },
  };
}

function localProfile() {
  const setupLedger = readJsonSync(setupLedgerPath) || {};
  const accounts = readJsonSync(accountsPath) || {};
  const sources = normalizeSetupSources(accounts).map((source) => ({
    id: source.id,
    label: source.label,
    status: source.status,
    linked: source.linked,
    skipped: source.skipped,
    usernames: source.usernames,
  }));
  return {
    operator: resolveOperator(setupLedger, accounts),
    accounts: {
      ...fileSummary(accountsPath),
      linkedCount: sources.filter((source) => source.linked).length,
      skippedCount: sources.filter((source) => source.skipped).length,
      sources,
    },
  };
}

const setupJobs = new Map<string, SetupJob>();
let cachedWhatsAppLinkStatus: { expiresAt: number; value: Record<string, any> } | null = null;
let cachedDuckdbTables: { key: string; expiresAt: number; value: Array<{ name: string; rows: number; vectorRows?: number; vectorPeople?: number }> } | null = null;
let cachedIndexEstimate: { key: string; expiresAt: number; value: Record<string, any> } | null = null;
let cachedIndexCoverage: { key: string; expiresAt: number; value: Record<string, any> } | null = null;
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

function sendBinary(res: any, data: Buffer, contentType: string, status = 200) {
  res.statusCode = status;
  res.setHeader("Content-Type", contentType);
  res.setHeader("Cache-Control", "no-store");
  res.end(data);
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
  const env: NodeJS.ProcessEnv = {
    ...readEnvSummary(),
    ...process.env,
    POWERPACKS_REPO_ROOT: powerpacksRepoRoot,
    PYTHONUNBUFFERED: "1",
  };
  const localBin = env.HOME ? path.join(env.HOME, ".local", "bin") : "";
  env.PATH = Array.from(new Set([
    localBin,
    "/opt/homebrew/bin",
    "/usr/local/bin",
    ...(env.PATH || "").split(":"),
  ].filter(Boolean))).join(":");
  return env;
}

function whatsAppProvider(): "wacli" | "waha" {
  const value = String(setupProcessEnv().POWERPACKS_WHATSAPP_PROVIDER || "wacli").trim().toLowerCase();
  return value === "waha" ? "waha" : "wacli";
}

function whatsAppQrPngRelativePath(): string {
  return whatsAppProvider() === "waha" ? whatsAppWahaQrPngRelativePath : whatsAppWacliQrPngRelativePath;
}

function removeLocalFiles(paths: string[]): string[] {
  const removed: string[] = [];
  for (const filePath of paths) {
    try {
      fs.unlinkSync(filePath);
      removed.push(path.relative(powerpacksRepoRoot, filePath));
    } catch (err) {
      if ((err as NodeJS.ErrnoException).code !== "ENOENT") throw err;
    }
  }
  return removed;
}

function wahaRuntimeCommand(command: "up" | "status", extra: string[] = []): string[] {
  return [
    "uv", "run", "--project", ".", "python",
    "packs/messages/primitives/waha_runtime/waha_runtime.py",
    command,
    "--engine", whatsAppWahaEngine,
    "--image", whatsAppWahaImage,
    ...extra,
  ];
}

function wahaSessionCommand(command: "start" | "status", extra: string[] = []): string[] {
  return [
    "uv", "run", "--project", ".", "python",
    "packs/messages/primitives/waha_session/waha_session.py",
    command,
    "--engine", whatsAppWahaEngine,
    ...extra,
  ];
}

function wahaWaitTimeoutSeconds(): string {
  const raw = String(setupProcessEnv().POWERPACKS_WAHA_WAIT_TIMEOUT || "180").trim();
  const seconds = Number.parseInt(raw, 10);
  return Number.isFinite(seconds) && seconds > 0 ? String(seconds) : "180";
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

function setupStageSummaries(stages: Array<{ label: string; command: string[] }>): SetupJobStage[] {
  const total = stages.length;
  return stages.map((stage, index) => ({
    label: stage.label,
    index: index + 1,
    total,
  }));
}

function stagedCommand(stages: Array<{ label: string; command: string[] }>): string[] {
  if (stages.length === 0) return [];
  if (stages.length === 1) return stages[0].command;
  const totalStages = stages.length;
  const stageSummaries = setupStageSummaries(stages);
  const script = [
    "set -o pipefail",
    ...stages.map((stage, index) => {
      const stageNumber = index + 1;
      const failurePayload = {
        status: "failed",
        failed_stage: stage.label,
        stage_index: stageNumber,
        total_stages: totalStages,
      };
      return [
        `printf '%s\\n' ${shellQuote(`setup: ${stage.label} (${stageNumber}/${totalStages})`)}`,
        shellJoin(stage.command),
        "code=$?",
        `if [ "$code" -ne 0 ]; then printf '%s\\n' ${shellQuote(JSON.stringify(failurePayload))}; exit "$code"; fi`,
      ].join("; ");
    }),
    `printf '%s\\n' ${shellQuote(JSON.stringify({ status: "completed", stages: stageSummaries }))}`,
  ].join("; ");
  return [
    "/bin/zsh",
    "-lc",
    script,
  ];
}

function importAndFanInCommand(importCommand: string[], fanInCommand: string[], label: string): string[] {
  return importsAndFanInCommand([{ label, command: importCommand }], fanInCommand);
}

function importsAndFanInCommand(imports: Array<{ label: string; command: string[] }>, fanInCommand: string[]): string[] {
  const importNetworkApprove = [
    "uv", "run", "--project", ".", "python",
    "packs/ingestion/primitives/discover_contacts_pipeline/discover_contacts_pipeline.py",
    "approve",
    "--ledger", importRefreshLedgerPath,
  ];
  const importNetworkContinue = [
    "uv", "run", "--project", ".", "python",
    "packs/ingestion/primitives/discover_contacts_pipeline/discover_contacts_pipeline.py",
    "continue",
    "--ledger", importRefreshLedgerPath,
  ];
  return [
    "/bin/zsh",
    "-lc",
    [
      ...imports.flatMap((stage) => [
        `printf '%s\\n' ${shellQuote(`setup: ${stage.label}`)}`,
        `${shellJoin(stage.command)}; code=$?`,
        [
          "if [[ $code -eq 20 ]]; then",
          `  printf '%s\\n' ${shellQuote("setup: approving network import spend")}`,
          `  ${shellJoin(importNetworkApprove)} && ${shellJoin(importNetworkContinue)}`,
          "elif [[ $code -ne 0 ]]; then",
          "  exit $code",
          "fi",
        ].join("\n"),
      ]),
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

function startSetupJob(action: string, command: string[], timeoutMs = 6 * 60 * 60 * 1000, metadata: Pick<SetupJob, "actionKey" | "source" | "stages"> = {}): SetupJob {
  pruneSetupJobs();
  const job: SetupJob = {
    id: randomUUID(),
    action,
    ...metadata,
    status: "running",
    startedAt: new Date().toISOString(),
    completedAt: null,
    command,
    code: null,
    stdout: "",
    stderr: "",
    log: "",
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
    const text = chunk.toString();
    job.stdout = `${job.stdout || ""}${text}`;
    job.log = `${job.log || ""}${text}`;
  });
  child.stderr.on("data", (chunk) => {
    const text = chunk.toString();
    job.stderr = `${job.stderr || ""}${text}`;
    job.log = `${job.log || ""}${text}`;
  });
  child.on("error", (err) => {
    clearTimeout(timer);
    job.status = "failed";
    job.completedAt = new Date().toISOString();
    job.stderr = `${job.stderr || ""}${err.message}`;
    job.log = `${job.log || ""}${err.message}`;
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
    "uv run --project . python packs/ingestion/primitives/import_contacts_pipeline/import_contacts_pipeline.py ",
    "uv run --project . python packs/messages/primitives/import_contacts_pipeline/import_contacts_pipeline.py ",
    "uv run --project . python packs/messages/primitives/import_whatsapp_wacli/import_whatsapp_wacli.py auth",
    "uv run --project . python packs/messages/primitives/waha_runtime/waha_runtime.py ",
    "uv run --project . python packs/messages/primitives/waha_session/waha_session.py ",
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
  if (approvalType !== "parallel") {
    if (block.review_url) {
      return {
        ...block,
        review_url: "/setup/imessage/review",
        message: "Review Messages contacts in the inline setup app. Click Complete when done.",
      };
    }
    return block;
  }

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

function whatsappLinkStatus(config: Record<string, any> = {}): Record<string, any> {
  const nowMs = Date.now();
  if (cachedWhatsAppLinkStatus && cachedWhatsAppLinkStatus.expiresAt > nowMs) {
    return cachedWhatsAppLinkStatus.value;
  }

  const provider = whatsAppProvider();
  const configuredStore = String(config.store || whatsAppStorePath);
  const storePath = path.isAbsolute(configuredStore) ? configuredStore : path.join(powerpacksRepoRoot, configuredStore);
  const qrPage = fs.existsSync(whatsAppWacliQrHtmlPath) ? whatsAppWacliQrHtmlRelativePath : "";
  const qrPng = fs.existsSync(whatsAppWacliQrPngPath) ? whatsAppWacliQrPngRelativePath : "";
  const qrUpdatedAt = fs.existsSync(whatsAppWacliQrPngPath) ? fs.statSync(whatsAppWacliQrPngPath).mtime.toISOString() : "";
  const authenticated = config.authenticated === true || ["linked", "authenticated"].includes(String(config.status || ""));
  const base = {
    status: authenticated ? "authenticated" : "not_authenticated",
    authenticated,
    provider,
    engine: provider === "waha" ? whatsAppWahaEngine : undefined,
    image: provider === "waha" ? whatsAppWahaImage : undefined,
    store: provider === "wacli" ? configuredStore : undefined,
    store_exists: fs.existsSync(storePath),
    qr_page: authenticated ? "" : qrPage,
    qr_png: authenticated ? "" : qrPng,
    qr_updated_at: authenticated ? "" : qrUpdatedAt,
  };
  const value: Record<string, any> = provider === "waha" && !authenticated ? {
    ...base,
    qr_png: fs.existsSync(whatsAppWahaQrPngPath) ? whatsAppWahaQrPngRelativePath : "",
    qr_raw: fs.existsSync(whatsAppWahaQrTxtPath) ? whatsAppWahaQrTxtRelativePath : "",
    qr_updated_at: fs.existsSync(whatsAppWahaQrPngPath) ? fs.statSync(whatsAppWahaQrPngPath).mtime.toISOString() : "",
  } : base;
  cachedWhatsAppLinkStatus = { expiresAt: nowMs + 5000, value };
  return value;
}

function messagesLinkStatus(config: Record<string, any> = {}): Record<string, Record<string, any>> {
  const whatsappConfig = config.whatsapp && typeof config.whatsapp === "object" ? config.whatsapp as Record<string, any> : {};
  return {
    imessage: imessagePermissionStatus(),
    whatsapp: whatsappLinkStatus(whatsappConfig),
  };
}

function normalizeSetupSources(accounts: RunState | null) {
  const records = accountRecords(accounts);
  const messagesRecord = records.messages || {};
  const messagesConfig = messagesRecord.config && typeof messagesRecord.config === "object" ? messagesRecord.config : {};
  const messagesStatus = messagesLinkStatus(messagesConfig);
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
    artifactDir: ledger.artifact_dir || ledger.run_dir || "",
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
    artifactDir: ledger.artifact_dir || ledger.run_dir || "",
  };
}

function messagesImportLedgerStatus(fallback: string) {
  const ledger = readJsonSync(messagesLedgerPath) || {};
  const summary = fileSummary(messagesLedgerPath);
  const steps = ledger.steps && typeof ledger.steps === "object" ? ledger.steps : {};
  const importStepIds = [
    "check_imessage",
    "extract_imessage",
    "normalize_imessage",
    "extract_whatsapp",
    "normalize_whatsapp",
    "ensure_contacts",
  ];
  const importBlock = importStepIds
    .map((id) => steps[id])
    .find((step) => {
      const status = String(step?.status || "").toLowerCase();
      return status === "failed" || status === "blocked_user_action";
    });
  const contactsReady = Boolean(steps.ensure_contacts?.status === "completed" || ledger.artifacts?.contacts_csv || fs.existsSync(messagesContactsCsvPath));
  const rawStatus = String(ledger.status || fallback);
  const status = importBlock
    ? String(importBlock.status)
    : contactsReady || rawStatus === "selected_steps_completed"
      ? "completed"
      : rawStatus === "blocked_approval"
        ? fallback
        : rawStatus;
  return {
    status,
    updatedAt: ledger.updated_at || ledger.completed_at || summary.updatedAt || null,
    artifactDir: ledger.artifact_dir || ledger.run_dir || "",
  };
}

function discoverContactsCommand(operatorId: string, extra: string[] = []) {
  const command = [
    "uv", "run", "--project", ".", "python",
    "packs/ingestion/primitives/discover_contacts_pipeline/discover_contacts_pipeline.py",
    "run",
    "--from-accounts", ".powerpacks/ingestion/accounts.json",
    "--operator-id", operatorId,
    "--include-existing-artifacts",
    "--ledger", discoverContactsSetupLedger,
  ];
  command.push(...extra);
  return command;
}

function gmailDiscoveryCommand(): string[] {
  return [
    "uv", "run", "--project", ".", "python",
    "packs/ingestion/primitives/discover_contacts_pipeline/gmail.py",
    "discover",
    "--accounts", ".powerpacks/ingestion/accounts.json",
  ];
}

function linkedinDiscoveryCommand(): string[] {
  return [
    "uv", "run", "--project", ".", "python",
    "packs/ingestion/primitives/discover_contacts_pipeline/linkedin.py",
    "discover",
    "--accounts", ".powerpacks/ingestion/accounts.json",
  ];
}

function messagesDiscoveryCommand(): string[] {
  return [
    "uv", "run", "--project", ".", "python",
    "packs/ingestion/primitives/discover_contacts_pipeline/messages.py",
    "discover",
    "--accounts", ".powerpacks/ingestion/accounts.json",
  ];
}

function discoveryManifestState(sourceId: string, fallback: string) {
  const relPathBySource: Record<string, string> = {
    gmail: ".powerpacks/network-import/discover/gmail/manifest.json",
    linkedin_csv: ".powerpacks/network-import/discover/linkedin/manifest.json",
    messages: ".powerpacks/network-import/discover/messages/manifest.json",
  };
  const fallbackRelPathBySource: Record<string, string> = {
    messages: ".powerpacks/network-import/messages/manifest.json",
  };
  const relPath = relPathBySource[sourceId];
  if (!relPath) return { status: fallback, updatedAt: null as string | null, artifactDir: "" };
  const primaryPath = path.join(powerpacksRepoRoot, relPath);
  const fallbackRelPath = fallbackRelPathBySource[sourceId];
  const fallbackPath = fallbackRelPath ? path.join(powerpacksRepoRoot, fallbackRelPath) : "";
  const manifestPath = fs.existsSync(primaryPath) || !fallbackPath ? primaryPath : fallbackPath;
  const activeRelPath = manifestPath === primaryPath ? relPath : String(fallbackRelPath);
  const manifest = readJsonSync(manifestPath) || {};
  const summary = fileSummary(manifestPath);
  const rawStatus = String(manifest.status || fallback);
  return {
    status: rawStatus === "selected_steps_completed" ? "completed" : rawStatus,
    updatedAt: manifest.updated_at || manifest.completed_at || summary.updatedAt || null,
    artifactDir: path.dirname(activeRelPath),
  };
}

function enrichmentNetworkCommand(operatorId: string, sourceId: string, options: { approveSpend?: boolean } = {}): string[] {
  const source = sourceId === "linkedin_csv" ? "linkedin" : sourceId;
  if (!["gmail", "linkedin", "messages"].includes(source)) return [];
  const command = [
    "uv", "run", "--project", ".", "python",
    `packs/ingestion/primitives/import_contacts_pipeline/${source}.py`,
    "run",
    "--accounts", ".powerpacks/ingestion/accounts.json",
    "--operator-id", operatorId,
  ];
  if (options.approveSpend && source === "messages") command.push("--confirm-import");
  else if (options.approveSpend) command.push("--approve-parallel-spend");
  return command;
}

function processLocalNetworkCommand(operatorId: string): string[] {
  return [
    "uv", "run", "--project", ".", "python",
    "packs/indexing/primitives/index_contacts_pipeline/index_contacts_pipeline.py",
    "fan-in",
    "--operator-id", operatorId,
    "--accounts", ".powerpacks/ingestion/accounts.json",
  ];
}

function indexContactsCommand(operatorId: string, extra: string[] = []): string[] {
  return [
    "uv", "run", "--project", ".", "python",
    "packs/indexing/primitives/index_contacts_pipeline/index_contacts_pipeline.py",
    "run",
    "--operator-id", operatorId,
    "--accounts", ".powerpacks/ingestion/accounts.json",
    ...extra,
  ];
}

function messageImportCommand(source: ReturnType<typeof normalizeSetupSources>[number]) {
  const whatsapp = source.config.whatsapp && typeof source.config.whatsapp === "object" ? source.config.whatsapp as Record<string, any> : {};
  const imessage = source.config.imessage && typeof source.config.imessage === "object" ? source.config.imessage as Record<string, any> : {};
  const includeFlags = [];
  if (imessage.status !== "skipped") includeFlags.push("--include-imessage");
  if (whatsapp.status === "linked" || whatsapp.authenticated === true) includeFlags.push("--include-whatsapp");
  includeFlags.push("--include-contact-merge");
  const refreshFlags = [];
  if (includeFlags.includes("--include-imessage")) refreshFlags.push("--force-imessage");
  if (includeFlags.includes("--include-whatsapp")) refreshFlags.push("--force-whatsapp");
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

function messageEnrichmentCommand(accounts: RunState | null): string[] {
  return enrichmentNetworkCommand(resolveOperator(readJsonSync(setupLedgerPath) || {}, accounts).id, "messages");
}

function buildImportSources(accounts: RunState | null, operatorId: string): SetupImportSource[] {
  const sources = normalizeSetupSources(accounts);
  const byId = Object.fromEntries(sources.map((source) => [source.id, source]));
  const rows: SetupImportSource[] = [];
  const setupRefreshLedger = discoverContactsSetupLedger;
  const refreshState = ledgerStatus(path.join(powerpacksRepoRoot, setupRefreshLedger), "ready");
  const refreshLedgerData = readJsonSync(path.join(powerpacksRepoRoot, setupRefreshLedger)) || {};
  const refreshSteps = refreshLedgerData.steps || {};
  const sourceUpdatedAt = (sourceKey: string): string | null => {
    const step = refreshSteps[sourceKey];
    return step?.finished_at || step?.started_at || null;
  };

  const gmail = byId.gmail;
  const gmailState = discoveryManifestState("gmail", "ready");
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
    status: gmail?.skipped ? "skipped" : gmail?.linked ? gmailState.status : "not_linked",
    linked: Boolean(gmail?.linked),
    skipped: Boolean(gmail?.skipped),
    accountEmail: gmailAccounts.length === 1 ? gmailAccounts[0] : undefined,
    accountCount: gmailAccounts.length,
    updatedAt: gmail?.linked && !gmail?.skipped ? (gmailState.updatedAt || sourceUpdatedAt("gmail_msgvault") || refreshState.updatedAt) : null,
    artifactDir: gmail?.linked && !gmail?.skipped ? gmailState.artifactDir : "",
    command: gmailDiscoveryCommand(),
  });

  for (const id of ["linkedin_csv", "messages", "twitter"] as const) {
    const source = byId[id];
    const discoveryState = discoveryManifestState(id, source?.linked ? "ready" : source?.skipped ? "skipped" : "not_linked");
    const state = id === "messages"
      ? discoveryState
      : id === "linkedin_csv"
        ? discoveryState
      : refreshState;
    rows.push({
      id,
      sourceId: id,
      label: SETUP_SOURCE_LABELS[id],
      status: source?.skipped ? "skipped" : source?.linked ? state.status : "not_linked",
      linked: Boolean(source?.linked),
      skipped: Boolean(source?.skipped),
      updatedAt: source?.linked && !source?.skipped
        ? (id === "linkedin_csv" ? (state.updatedAt || sourceUpdatedAt("linkedin")) : state.updatedAt)
        : null,
      artifactDir: source?.linked && !source?.skipped ? state.artifactDir : "",
      runnable: id === "twitter" ? false : undefined,
      disabledReason: id === "twitter" ? "Twitter/X handle is recorded; follower import is not wired into setup yet." : undefined,
      command: id === "messages"
        ? messagesDiscoveryCommand()
        : id === "twitter"
          ? []
          : linkedinDiscoveryCommand(),
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
  const materializeMessagesPeople = enrichmentNetworkCommand("local", "messages");
  return ["/bin/zsh", "-lc", `${shellJoin(markAppReviewComplete)} && ${shellStage("materializing Messages people", materializeMessagesPeople)}`];
}

function messagesApproveAndContinueCommand(accounts: RunState | null): string[] {
  const ledger = readJsonSync(messagesLedgerPath) || {};
  const approve = [
    "uv", "run", "--project", ".", "python",
    "packs/messages/primitives/import_contacts_pipeline/import_contacts_pipeline.py",
    "approve",
    "--ledger", ".powerpacks/messages/import-run.setup-messages.json",
    "--confirm",
  ];
  const continueAfterApproval = [
    "uv", "run", "--project", ".", "python",
    "packs/messages/primitives/import_contacts_pipeline/import_contacts_pipeline.py",
    "continue",
    "--ledger", ".powerpacks/messages/import-run.setup-messages.json",
    "--parallel-timeout", String(process.env.POWERPACKS_SETUP_MESSAGES_PARALLEL_TIMEOUT_SECONDS || "900"),
    "--parallel-auto-approve-usd", "25",
    "--reuse-existing-artifacts",
    ...messageChannelFlags(accounts, ledger),
    "--include-contact-merge",
    "--include-powerset-candidates",
    "--include-local-match",
    "--include-llm-review",
    "--include-research",
    "--include-review",
    "--no-open-browser",
    "--force-prepare-queue",
  ];
  return ["/bin/zsh", "-lc", `${shellJoin(approve)} && ${shellJoin(continueAfterApproval)}`];
}

function csvPathCount(value: unknown): number {
  return csvRowsForArtifact(value).length;
}

function csvRowsForArtifact(value: unknown, preferredKeys: string[] = []): Record<string, string>[] {
  const paths = new Set<string>();
  const visit = (item: unknown) => {
    if (Array.isArray(item)) {
      item.forEach(visit);
      return;
    }
    if (item && typeof item === "object") {
      const record = item as Record<string, unknown>;
      const keys = preferredKeys.length ? preferredKeys : Object.keys(record);
      for (const key of keys) visit(record[key]);
      return;
    }
    if (typeof item !== "string" || !item.trim()) return;
    paths.add(item.trim());
  };
  visit(value);
  const rows: Record<string, string>[] = [];
  for (const item of paths) {
    const resolved = safeJoinPowerpacks(item);
    if (!resolved || !fs.existsSync(resolved)) continue;
    try {
      rows.push(...parseCsvDocument(fs.readFileSync(resolved, "utf8")).rows);
    } catch {
      // Ignore malformed or currently written artifacts in status summaries.
    }
  }
  return rows;
}

function firstCsvCount(...values: unknown[]): number {
  for (const value of values) {
    const count = csvPathCount(value);
    if (count > 0) return count;
  }
  return 0;
}

function csvCountForArtifactKeys(artifacts: Record<string, any>, pattern: RegExp): number {
  return Object.entries(artifacts).reduce((total, [key, value]) => {
    if (!pattern.test(key)) return total;
    return total + csvPathCount(value);
  }, 0);
}

function normalizedDigits(value: unknown): string {
  return String(value || "").replace(/\D+/g, "").slice(-10);
}

function normalizeNameKey(value: unknown): string {
  return String(value || "").trim().toLowerCase().replace(/\s+/g, " ");
}

function parseDirectoryConfidence(value: unknown, status: unknown): number {
  const raw = String(value || "").trim().toLowerCase();
  if (!raw) {
    const normalizedStatus = String(status || "").trim().toLowerCase();
    return ["completed", "found", "success"].includes(normalizedStatus) ? 0.9 : 0;
  }
  if (["high", "confirmed", "exact"].includes(raw)) return 0.95;
  if (["medium", "med"].includes(raw)) return 0.8;
  if (raw === "low") return 0.5;
  const parsed = Number(raw);
  if (!Number.isFinite(parsed)) return 0;
  return parsed > 1 ? parsed / 100 : parsed;
}

function directoryLookupMatchCount(artifact: unknown, directoryRows: Record<string, string>[], preferredKeys: string[] = []): number {
  const rows = csvRowsForArtifact(artifact, preferredKeys);
  if (!rows.length || !directoryRows.length) return 0;
  const eligibleDirectoryRows = directoryRows.filter((row) =>
    validLinkedInUrl(row.linkedin_url)
    && parseDirectoryConfidence(row.confidence, row.status) >= 0.75
  );
  if (!eligibleDirectoryRows.length) return 0;
  const emails = new Set(eligibleDirectoryRows.map((row) => String(row.email || "").trim().toLowerCase()).filter(Boolean));
  const phones = new Set(eligibleDirectoryRows.map((row) => normalizedDigits(row.phone)).filter(Boolean));
  const names = new Map<string, Set<string>>();
  for (const row of eligibleDirectoryRows) {
    const nameKey = normalizeNameKey(row.name || row.matched_name || "");
    const linkedinUrl = String(row.linkedin_url || "").trim().toLowerCase();
    if (!nameKey || !linkedinUrl) continue;
    const urls = names.get(nameKey) || new Set<string>();
    urls.add(linkedinUrl);
    names.set(nameKey, urls);
  }
  let matched = 0;
  for (const row of rows) {
    const rowValues = Object.entries(row);
    const hasEmailMatch = rowValues.some(([key, value]) => {
      if (!key.toLowerCase().includes("email")) return false;
      return emails.has(String(value || "").trim().toLowerCase());
    });
    const hasPhoneMatch = rowValues.some(([key, value]) => {
      const lower = key.toLowerCase();
      if (!lower.includes("phone") && lower !== "handle") return false;
      const digits = normalizedDigits(value);
      return Boolean(digits && phones.has(digits));
    });
    const nameKey = normalizeNameKey(
      row.display_name
      || row.full_name
      || row.matched_name
      || row.name
      || `${row.first_name || ""} ${row.last_name || ""}`.trim()
    );
    const hasUniqueNameMatch = Boolean(nameKey && names.get(nameKey)?.size === 1);
    if (hasEmailMatch || hasPhoneMatch || hasUniqueNameMatch) matched += 1;
  }
  return matched;
}

function validLinkedInUrl(value: unknown): boolean {
  const text = String(value || "").trim();
  return /^https?:\/\/(?:[\w-]+\.)?linkedin\.com\/in\//i.test(text);
}

function messagesReviewStats(messagesLedger: RunState): { total: number; enrich: number; skipped: number; matched: number; profilesFound: number } {
  const reviewRows = csvRowsForArtifact(".powerpacks/messages/research_review.csv");
  const llmSummary = messagesLedger.steps?.llm_review?.summary || {};
  const llmCounts = llmSummary.counts || {};
  const matchStats = messagesLedger.steps?.match_local_contacts?.summary?.stats || {};
  const reviewMatched = reviewRows.filter((row) => String(row.network_match_status || "").toLowerCase() === "matched").length;
  const reviewProfiles = reviewRows.filter((row) =>
    String(row.network_match_status || "").toLowerCase() === "matched"
    && (validLinkedInUrl(row.linkedin_url) || validLinkedInUrl(row.network_linkedin_url))
  ).length;
  const matched = Math.max(Number(matchStats.matched || 0), reviewMatched);
  const enrich = Number(llmCounts.enrich || 0);
  const skipped = Number(llmCounts.skip || 0);
  return {
    total: Number(llmSummary.candidate_count || llmCounts.verdicts || enrich + skipped || 0),
    enrich,
    skipped,
    matched,
    profilesFound: Math.max(matched, reviewProfiles),
  };
}

function sha256File(filePath: string): string {
  try {
    return createHash("sha256").update(fs.readFileSync(filePath)).digest("hex");
  } catch {
    return "";
  }
}

function localSearchRecordSummary(): { recordFiles: number; nonemptyRecordFiles: number } {
  const recordsDir = path.join(powerpacksStateRoot, "search-index", "records");
  if (!fs.existsSync(recordsDir)) return { recordFiles: 0, nonemptyRecordFiles: 0 };
  const files = fs.readdirSync(recordsDir).filter((file) => file.endsWith(".records.jsonl"));
  const nonempty = files.filter((file) => {
    try {
      return fs.statSync(path.join(recordsDir, file)).size > 0;
    } catch {
      return false;
    }
  });
  return { recordFiles: files.length, nonemptyRecordFiles: nonempty.length };
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

function maybeBuildLocalDuckdbFromBootstrapRecords(operatorId: string, duckdbPath: string, existingTables: Array<{ name: string; rows: number }>): Record<string, any> | null {
  const records = localSearchRecordSummary();
  const duckdbExists = fs.existsSync(duckdbPath);
  const duckdbHasRows = existingTables.some((table) => Number(table.rows || 0) > 0);
  if (!records.nonemptyRecordFiles || (duckdbExists && duckdbHasRows)) return null;
  const result = spawnSync("uv", [
    "run", "--project", ".", "python",
    "scripts/build-local-duckdb-shim.py",
    "--records-dir", ".powerpacks/search-index",
    "--operator-id", operatorId,
    "--force",
  ], {
    cwd: powerpacksRepoRoot,
    env: setupProcessEnv(),
    encoding: "utf8",
    timeout: 60 * 60 * 1000,
  });
  cachedDuckdbTables = null;
  const payload = parseJsonFragment(result.stdout || "") || {};
  return {
    status: result.status === 0 ? "ok" : "failed",
    records,
    ...payload,
    error: result.status === 0 ? "" : String(payload.error || result.stderr || result.error?.message || "").slice(0, 1000),
  };
}

function localIndexCoverage(peopleSha256: string, duckdbPath: string): Record<string, any> {
  const peopleCsv = path.join(powerpacksStateRoot, "network-import", "merged", "people.csv");
  if (!fs.existsSync(peopleCsv)) {
    return { status: "missing_people_csv", totalPeople: 0, indexedPeople: 0, pendingPeople: 0, existingDuckdbKeys: 0 };
  }
  const duckdbStat = fs.existsSync(duckdbPath) ? fs.statSync(duckdbPath) : null;
  const key = `${peopleSha256 || sha256File(peopleCsv)}:${duckdbStat?.mtimeMs || 0}:${duckdbStat?.size || 0}`;
  const nowMs = Date.now();
  if (cachedIndexCoverage && cachedIndexCoverage.key === key && cachedIndexCoverage.expiresAt > nowMs) {
    return cachedIndexCoverage.value;
  }
  const script = `
import importlib.util, json
from pathlib import Path
module_path = Path("packs/indexing/primitives/build_processing_pipeline/build_processing_pipeline.py")
spec = importlib.util.spec_from_file_location("build_processing_pipeline_status", module_path)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
people = mod.flatten_people(Path(".powerpacks/network-import/merged/people.csv"))
indexed = 0
pending = 0
processed_ids = mod._processed_person_ids(Path(".powerpacks/search-index"))
for person in people:
    person_id = mod._person_id(person)
    if person_id and person_id in processed_ids:
        indexed += 1
    else:
        pending += 1
print(json.dumps({
    "status": "ok",
    "totalPeople": len(people),
    "indexedPeople": indexed,
    "pendingPeople": pending,
    "processedPersonIds": len(processed_ids),
}))
`;
  const result = spawnSync("uv", ["run", "--project", ".", "python", "-c", script], {
    cwd: powerpacksRepoRoot,
    env: setupProcessEnv(),
    encoding: "utf8",
    timeout: 10000,
  });
  let value: Record<string, any>;
  try {
    value = JSON.parse(result.stdout || "{}");
  } catch {
    value = {};
  }
  if (!value.status) {
    value = {
      status: result.status === 0 ? "ok" : "failed",
      totalPeople: 0,
      indexedPeople: 0,
      pendingPeople: 0,
      existingDuckdbKeys: 0,
      error: String(result.stderr || result.error?.message || "").slice(0, 1000),
    };
  }
  cachedIndexCoverage = { key, expiresAt: nowMs + 30000, value };
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

function lastIndexProcessingEstimate(peopleSha256: string): Record<string, any> {
  const manifest = readJsonSync(path.join(powerpacksStateRoot, "network-import", "index", "contacts", "manifest.json")) || {};
  const estimate = manifest.processing_estimate || {};
  const manifestPeopleSha = String(manifest.people_sha256 || "");
  const stale = Boolean(peopleSha256 && manifestPeopleSha && manifestPeopleSha !== peopleSha256);
  if (!estimate || typeof estimate !== "object" || !Object.keys(estimate).length) {
    return {
      status: "not_loaded",
      totalEstimatedUsd: 0,
      estimatedPaidCalls: {},
      counts: {},
      providers: {},
      stale,
      error: "",
    };
  }
  return {
    status: estimate.status || "last_dry_run",
    totalEstimatedUsd: Number(estimate.total_estimated_usd ?? estimate.estimated_cost_usd ?? 0),
    estimatedPaidCalls: estimate.estimated_paid_calls || {},
    counts: estimate.counts || {},
    providers: estimate.providers || {},
    stale,
    source: "last_index_manifest",
    error: "",
  };
}

function readRelativeLedger(relativePath: string): RunState {
  return readJsonSync(path.join(powerpacksRepoRoot, relativePath)) || {};
}

function importManifest(sourceId: string): RunState {
  const source = sourceId === "linkedin_csv" ? "linkedin" : sourceId;
  return readJsonSync(path.join(powerpacksStateRoot, "network-import", "import", source, "manifest.json")) || {};
}

function buildEnrichmentSources(setupSources: ReturnType<typeof normalizeSetupSources> = []): SetupEnrichmentSource[] {
  const gmailImport = importManifest("gmail");
  const linkedinImport = importManifest("linkedin");
  const messagesImport = importManifest("messages");
  const gmailImportArtifacts = gmailImport.artifacts || {};
  const linkedinImportArtifacts = linkedinImport.artifacts || {};
  const linkedInStep = linkedinImport.steps?.enrich_people || {};
  const gmailDirectoryStep = gmailImport.steps?.gmail_directory || {};
  const gmailResolutionStep = gmailImport.steps?.gmail_linkedin_resolution || {};
  const gmailApplyStep = gmailImport.steps?.gmail_apply_enrich || {};
  const sourceById = Object.fromEntries(setupSources.map((source) => [source.id, source]));
  const isSkipped = (id: string) => Boolean(sourceById[id]?.skipped);
  const statsNumber = (manifest: Record<string, any>, ...keys: string[]) => {
    for (const key of keys) {
      const value = Number(manifest.stats?.[key]);
      if (Number.isFinite(value) && value > 0) return value;
    }
    return 0;
  };
  const gmailArtifacts = gmailImportArtifacts;
  const linkedinArtifacts = linkedinImportArtifacts;
  const gmailDirectoryEntries = Object.values((gmailArtifacts.gmail_directory_by_slug || {}) as Record<string, any>);
  const gmailExistingMatches = statsNumber(gmailImport, "existing_matches", "matched")
    || gmailDirectoryEntries.reduce((total, item) => total + Number(item?.resolved || 0), 0);
  const gmailUnresolvedRows = statsNumber(gmailImport, "unresolved", "remaining_unresolved")
    || gmailDirectoryEntries.reduce((total, item) => total + Number(item?.unresolved || 0), 0);
  const gmailNotFound = statsNumber(gmailImport, "not_found", "skipped", "failed");
  const gmailProviderEnriched = statsNumber(gmailImport, "profiles_found", "people");
  const linkedinCacheHits = statsNumber(linkedinImport, "cache_hits", "existing_matches")
    || Number(linkedInStep.summary?.cache_hit_count || 0);
  const linkedinEnriched = statsNumber(linkedinImport, "profiles_found", "people")
    || Number(linkedInStep.summary?.people || 0);

  const gmailBlocked = gmailResolutionStep.status === "blocked" || gmailApplyStep.status === "blocked";
  const COST_PER_1000_CORE2X = 50;
  const gmailEstimatedCostUsd = gmailUnresolvedRows > 0 ? Math.round(gmailUnresolvedRows * COST_PER_1000_CORE2X) / 1000 : null;

  const rowsById: Record<string, SetupEnrichmentSource> = {
    linkedin_csv: {
      id: "linkedin_csv",
      label: "LinkedIn",
      status: String(linkedinImport.status || linkedInStep.status || "unknown"),
      candidates: Number(linkedinImport.stats?.candidates || linkedInStep.summary?.queue_count || 0),
      enriched: linkedinEnriched,
      skipped: statsNumber(linkedinImport, "not_found", "skipped", "failed")
        || Number(linkedInStep.summary?.recent_failure_count || 0),
      matched: linkedinCacheHits,
      unresolved: 0,
      estimatedCostUsd: null,
      blocked: String(linkedinImport.status || "").startsWith("blocked"),
      updatedAt: isSkipped("linkedin_csv") ? null : linkedinImport.updated_at || linkedInStep.finished_at || null,
    },
    gmail: {
      id: "gmail",
      label: "Gmail",
      status: gmailBlocked ? "blocked" : String(gmailImport.status || "unknown"),
      candidates: Number(gmailImport.stats?.candidates || 0),
      enriched: gmailProviderEnriched,
      skipped: gmailNotFound,
      matched: gmailExistingMatches,
      unresolved: gmailUnresolvedRows,
      estimatedCostUsd: gmailEstimatedCostUsd,
      blocked: gmailBlocked,
      updatedAt: isSkipped("gmail") ? null : gmailImport.updated_at || gmailApplyStep.finished_at || gmailResolutionStep.finished_at || gmailDirectoryStep.finished_at || null,
    },
    messages: {
      id: "messages",
      label: "Messages",
      status: String(messagesImport.status || "unknown"),
      candidates: Number(messagesImport.stats?.candidates || 0),
      enriched: Number(messagesImport.stats?.people || 0),
      skipped: 0,
      matched: 0,
      unresolved: 0,
      estimatedCostUsd: null,
      blocked: String(messagesImport.status || "").startsWith("blocked"),
      updatedAt: isSkipped("messages") ? null : messagesImport.updated_at || null,
    },
    twitter: {
      id: "twitter",
      label: "Twitter/X",
      status: isSkipped("twitter") ? "skipped" : "unknown",
      candidates: 0,
      enriched: 0,
      skipped: 0,
      matched: 0,
      unresolved: 0,
      estimatedCostUsd: null,
      blocked: false,
      updatedAt: null,
    },
  };

  return SETUP_SOURCE_ORDER
    .filter((id) => rowsById[id])
    .map((id) => rowsById[id]);
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

async function setupStatus(tabInput: unknown = "all") {
  const tab = normalizeSetupStatusTab(tabInput);
  const includeDiscover = tab === "all" || tab === "discover" || tab === "enrichment";
  const includeEnrichment = tab === "all" || tab === "enrichment";
  const includeIndex = tab === "all" || tab === "index";
  const includeReview = includeEnrichment;
  const setupLedger = readJsonSync(setupLedgerPath) || {};
  const accounts = readJsonSync(accountsPath) || {};
  const importRefreshLedger = readJsonSync(importRefreshLedgerPath) || {};
  const messagesLedger = includeReview ? readJsonSync(messagesLedgerPath) || {} : {};
  const reviewPath = includeReview ? resolveReviewCsvPath() || messagesReviewCsvPath : messagesReviewCsvPath;
  const reviewApi = includeReview ? messagesReviewResponse("all", "", 0, 1) : { counts: {} };
  const phases = setupLedger.phases || {};
  const sources = normalizeSetupSources(accounts);
  const linkedSources = sources.filter((source) => source.linked).map((source) => source.id);
  const skippedSources = sources.filter((source) => source.skipped).map((source) => source.id);
  const unresolvedSources = sources.filter((source) => !source.linked && !source.skipped).map((source) => source.id);
  const operator = resolveOperator(setupLedger, accounts);
  const importSources = includeDiscover ? buildImportSources(accounts, operator.id) : [];
  const enrichmentSources = includeEnrichment ? buildEnrichmentSources(sources) : [];
  const bootstrap = bootstrapSummary(operator);
  const setupFile = fileSummary(setupLedgerPath);
  const importFile = includeDiscover ? fileSummary(importRefreshLedgerPath) : { path: path.relative(powerpacksRepoRoot, importRefreshLedgerPath), exists: fs.existsSync(importRefreshLedgerPath) };
  const indexPhase = phases.index || {};
  const peopleCsvPath = path.join(powerpacksStateRoot, "network-import", "merged", "people.csv");
  const duckdbPath = path.join(powerpacksStateRoot, "search-index", "local-search.duckdb");
  const peopleSha256 = includeIndex ? String(indexPhase.people_sha256 || sha256File(peopleCsvPath) || "") : String(indexPhase.people_sha256 || "");
  let duckdbFile = includeIndex ? fileSummary(duckdbPath) : { path: path.relative(powerpacksRepoRoot, duckdbPath), exists: fs.existsSync(duckdbPath) };
  let duckdbTables = includeIndex ? localDuckdbTableCounts(duckdbPath) : [];
  const duckdbRepair = null;
  if (includeIndex) {
    duckdbFile = fileSummary(duckdbPath);
    duckdbTables = localDuckdbTableCounts(duckdbPath);
  }
  const duckdbHasRows = duckdbTables.some((table) => Number(table.rows || 0) > 0);
  const bootstrapRecords = includeIndex ? localSearchRecordSummary() : { recordFiles: 0, nonemptyRecordFiles: 0 };
  const bootstrapRestorePreferred = Number(bootstrap.peopleRecords || 0) > 0
    && Boolean(bootstrapRecords.nonemptyRecordFiles)
    && (!duckdbFile.exists || !duckdbHasRows);
  const processingEstimate = !includeIndex ? {
    status: "not_loaded",
    totalEstimatedUsd: 0,
    estimatedPaidCalls: {},
    counts: {},
    providers: {},
    error: "",
  } : bootstrapRestorePreferred ? {
    status: "local_records_restore",
    totalEstimatedUsd: 0,
    estimatedPaidCalls: {},
    counts: { people: Number(bootstrap.peopleRecords || 0) },
    providers: {},
    error: "",
  } : lastIndexProcessingEstimate(peopleSha256);
  const indexCoverage = includeIndex ? localIndexCoverage(peopleSha256, duckdbPath) : { status: "not_loaded", totalPeople: 0, indexedPeople: 0, pendingPeople: 0 };
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
      artifactDir: importLiveRefresh?.artifact_dir || importRefreshLedger.artifact_dir || "",
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
      peopleRecords: includeIndex ? csvPathCount(".powerpacks/network-import/merged/people.csv") : 0,
      peopleSha256,
      readiness: indexPhase.status || "unknown",
      reason: indexPhase.reason || "",
      indexInputSha256: indexPhase.index_input_sha256 || "",
      bootstrapRecords,
      duckdbRepair,
      coverage: indexCoverage,
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
  const accounts = readJsonSync(accountsPath) || {};
  const operator = resolveOperator(setupLedger, accounts);
  const action = requireString(body.action, "action");

  if (action === "run-command") {
    return startWhitelistedShellJob(requireString(body.command, "command"));
  }

  if (action === "import") {
    const importableSources = buildImportSources(accounts, operator.id)
      .filter((source) => source.linked && !source.skipped && source.runnable !== false && source.command.length > 0);
    if (!importableSources.length) throw new Error("no linked sources can be discovered");
    const stages = importableSources.map((source) => ({
      label: `Discovering ${source.label}`,
      command: source.command,
    }));
    return startSetupJob(action, stagedCommand(stages), 6 * 60 * 60 * 1000, {
      stages: setupStageSummaries(stages),
    });
  }

  if (action === "index") {
    return startSetupJob(action, indexContactsCommand(operator.id));
  }

  if (["bootstrap", "link", "run"].includes(action)) {
    return startSetupJob(action, setupCommandArgs(operator.id, action as any));
  }

  if (action === "enrich-all") {
    const enrichableSources = buildImportSources(accounts, operator.id)
      .filter((source) => source.linked && !source.skipped && source.runnable !== false && source.id !== "twitter");
    if (!enrichableSources.length) throw new Error("no linked sources can be enriched");
    const stages = enrichableSources
      .map((source) => ({
        label: `Enriching ${source.label}`,
        command: enrichmentNetworkCommand(operator.id, source.id),
      }))
      .filter((stage) => stage.command.length > 0);
    return startSetupJob(action, stagedCommand(stages), 6 * 60 * 60 * 1000, {
      stages: setupStageSummaries(stages),
    });
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
    return startSetupJob(action, source.command, 6 * 60 * 60 * 1000, {
      actionKey: `${action}:${sourceId}`,
      source: sourceId,
    });
  }

  if (action === "enrich-source") {
    const sourceId = requireString(body.source, "source");
    const approveSpend = body.approveSpend === true;
    const source = buildImportSources(accounts, operator.id).find((candidate) => candidate.id === sourceId);
    if (!source) throw new Error(`unsupported import source: ${sourceId}`);
    if (!source.linked) throw new Error(`source is not linked: ${sourceId}`);
    if (source.skipped) throw new Error(`source is skipped: ${sourceId}`);
    if (source.runnable === false || source.command.length === 0) {
      throw new Error(source.disabledReason || `source is not importable yet: ${sourceId}`);
    }
    const command = enrichmentNetworkCommand(operator.id, sourceId, { approveSpend });
    if (command.length === 0) {
      throw new Error(`source enrichment is not wired yet: ${sourceId}`);
    }
    return startSetupJob(action, stagedCommand([{ label: `Enriching ${source.label}`, command }]), 6 * 60 * 60 * 1000, {
      actionKey: `${action}:${sourceId}${approveSpend ? ":approve" : ""}`,
      source: sourceId,
    });
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
    if (whatsAppProvider() === "waha") {
      removeLocalFiles([whatsAppWahaQrPngPath, whatsAppWahaQrTxtPath]);
      const runtimeUp = wahaRuntimeCommand("up");
      const sessionStart = wahaSessionCommand("start", [
        "--force",
        "--open",
        "--wait",
        "--wait-timeout", wahaWaitTimeoutSeconds(),
      ]);
      return startSetupJob(action, [
        "/bin/zsh", "-lc",
        `${shellJoin(runtimeUp)} && ${shellJoin(sessionStart)}`,
      ], 10 * 60 * 1000);
    }
    removeLocalFiles([whatsAppWacliQrPngPath, whatsAppWacliQrHtmlPath]);
    return startSetupJob(action, [
      "uv", "run", "--project", ".", "python",
      "packs/messages/primitives/import_whatsapp_wacli/import_whatsapp_wacli.py",
      "auth",
      "--store", ".powerpacks/messages/wacli",
      "--no-open-qr-page",
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

export function powerpacksLocalApiPlugin(): Plugin {
  return {
    name: "powerpacks-local-api",
    configureServer(server) {
      server.middlewares.use(async (req, res, next) => {
        try {
          const url = new URL(req.url || "/", "http://localhost");
          if (url.pathname === "/local-api/runs") {
            return sendJson(res, await listRuns());
          }

          if (url.pathname === "/local-api/profile") {
            return sendJson(res, localProfile());
          }

          if (url.pathname === "/local-api/env/status") {
            return sendJson(res, envStatus());
          }

          if (url.pathname === "/local-api/setup/status") {
            return sendJson(res, await setupStatus(url.searchParams.get("tab") || url.searchParams.get("scope")));
          }

          if (url.pathname === "/local-api/setup/whatsapp-qr") {
            const relativePath = String(url.searchParams.get("path") || whatsAppQrPngRelativePath());
            const resolved = safeJoinPowerpacks(relativePath);
            const messagesDir = `${path.join(powerpacksStateRoot, "messages")}${path.sep}`;
            if (!resolved || !resolved.startsWith(messagesDir) || path.extname(resolved).toLowerCase() !== ".png" || !fs.existsSync(resolved)) {
              return sendJson(res, { error: "WhatsApp QR not found" }, 404);
            }
            return sendBinary(res, fs.readFileSync(resolved), "image/png");
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
