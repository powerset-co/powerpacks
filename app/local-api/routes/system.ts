import { spawn } from "child_process";
import { existsSync, mkdirSync, openSync, readFileSync } from "fs";
import { homedir } from "os";
import { join } from "path";

import { sendJson } from "../lib/http";
import { powerpacksRepoRoot } from "../lib/paths";
import { readEnvSummary } from "../lib/env";
import { startSetupJob } from "../jobs";

// One fixed port contract, matching scripts/run-powerpacks-console.sh.
const PORT = process.env.PORT || "5177";
const CONSOLE_SCRIPT = "scripts/run-powerpacks-console.sh";

type RunResult = { code: number | null; stdout: string; stderr: string };

function run(cmd: string, args: string[], opts: { timeoutMs?: number } = {}): Promise<RunResult> {
  return new Promise((resolve) => {
    const child = spawn(cmd, args, { cwd: powerpacksRepoRoot, shell: false });
    let stdout = "";
    let stderr = "";
    const timer = setTimeout(() => child.kill("SIGTERM"), opts.timeoutMs ?? 30000);
    child.stdout.on("data", (d) => (stdout += d.toString()));
    child.stderr.on("data", (d) => (stderr += d.toString()));
    child.on("error", (err) => {
      clearTimeout(timer);
      resolve({ code: null, stdout, stderr: `${stderr}${err instanceof Error ? err.message : String(err)}` });
    });
    child.on("close", (code) => {
      clearTimeout(timer);
      resolve({ code, stdout, stderr });
    });
  });
}

// Which agent hosts are installed, so "update" runs the right updater(s).
function detectHosts(): { claude: boolean; codex: boolean } {
  const home = homedir();
  return {
    claude: existsSync(join(home, ".claude", "skills", "search-network")),
    codex: existsSync(join(home, ".codex", "powerpacks")) || existsSync(join(home, ".codex", "skills")),
  };
}

function readVersions(): { powerpacks: string; console: string } {
  let powerpacks = "";
  let consoleVersion = "";
  try {
    const py = readFileSync(join(powerpacksRepoRoot, "pyproject.toml"), "utf8");
    powerpacks = (py.match(/^version\s*=\s*"([^"]+)"/m) || [])[1] || "";
  } catch {
    powerpacks = "";
  }
  try {
    const pkg = JSON.parse(readFileSync(join(powerpacksRepoRoot, "app", "package.json"), "utf8"));
    consoleVersion = typeof pkg.version === "string" ? pkg.version : "";
  } catch {
    consoleVersion = "";
  }
  return { powerpacks, console: consoleVersion };
}

// Remote-latest via `git fetch` (the user's chosen route). Read-only; no spend.
async function updateStatus() {
  await run("git", ["fetch", "--quiet", "origin"], { timeoutMs: 30000 });
  const branch = (await run("git", ["rev-parse", "--abbrev-ref", "HEAD"])).stdout.trim();
  const current = (await run("git", ["rev-parse", "HEAD"])).stdout.trim();
  const latest = (await run("git", ["rev-parse", "origin/main"])).stdout.trim();
  const behind = parseInt((await run("git", ["rev-list", "--count", "HEAD..origin/main"])).stdout.trim() || "0", 10) || 0;
  const dirty = (await run("git", ["status", "--porcelain"])).stdout.trim().length > 0;
  return {
    branch,
    current_hash: current,
    latest_hash: latest,
    short_current: current.slice(0, 7),
    short_latest: latest.slice(0, 7),
    behind,
    dirty,
    update_available: behind > 0,
    versions: readVersions(),
    hosts: detectHosts(),
    checked_at: new Date().toISOString(),
  };
}

// Run the host updater(s): git pull --ff-only + sync-agent-files + install.sh.
function startUpdate() {
  const hosts = detectHosts();
  const steps: string[] = [];
  if (hosts.claude) steps.push("bin/update-claude-code");
  if (hosts.codex) steps.push("bin/update-codex");
  if (!steps.length) steps.push("git pull --ff-only");
  const job = startSetupJob("system-update", ["/bin/bash", "-lc", steps.join(" && ")]);
  // FE chains a restart on success (the user wants auto-restart after update).
  return { job, hosts, steps, auto_restart: true };
}

async function daemonStatus() {
  const result = await run("bash", [CONSOLE_SCRIPT, "daemon-status"], { timeoutMs: 15000 });
  const pidMatch = result.stdout.match(/pid = (\d+)/);
  return {
    daemonized: result.code === 0,
    running: /LISTEN/.test(result.stdout),
    pid: pidMatch ? parseInt(pidMatch[1], 10) : null,
    port: PORT,
    raw: result.stdout.trim(),
  };
}

// The "yank the batteries and fall on it" reboot. ALWAYS does the same thing:
// free the port (kill whatever serves it, including ourselves) then re-install
// the launchd daemon (KeepAlive = self-recovery). Detached + own session so
// killing our own Vite parent does not abort the relaunch. Idempotent: click
// it repeatedly and it restarts cleanly each time.
function startRestart() {
  const logDir = join(powerpacksRepoRoot, ".powerpacks", "servers");
  mkdirSync(logDir, { recursive: true });
  const fd = openSync(join(logDir, "self-restart.log"), "a");
  const script = [
    "set -x",
    "sleep 1", // let the HTTP response flush before we kill the server
    `echo "[$(date)] self-restart begin pid=$$"`,
    `bash ${CONSOLE_SCRIPT} stop || true`,
    `lsof -ti tcp:${PORT} | xargs kill 2>/dev/null || true`,
    "sleep 1",
    `bash ${CONSOLE_SCRIPT} daemon-install`,
    `echo "[$(date)] self-restart done"`,
  ].join("\n");
  const child = spawn("/bin/bash", ["-lc", script], {
    cwd: powerpacksRepoRoot,
    detached: true,
    stdio: ["ignore", fd, fd],
  });
  child.unref();
  return { restarting: true, url: `http://localhost:${PORT}`, port: PORT };
}

// Powerset login, read straight from the credentials file. We only touch
// `email` and `expires_at` — never the tokens (privacy contract).
function loginStatus() {
  const credPath = process.env.POWERPACKS_CREDENTIALS_PATH || join(homedir(), ".powerpacks", "credentials.json");
  try {
    const creds = JSON.parse(readFileSync(credPath, "utf8"));
    const expiresAt = Number(creds.expires_at || 0);
    const loggedIn = expiresAt > Date.now() / 1000;
    return {
      logged_in: loggedIn,
      email: typeof creds.email === "string" ? creds.email : "",
      expires_at: expiresAt,
      expired: expiresAt > 0 && !loggedIn,
    };
  } catch {
    return { logged_in: false, email: "", expires_at: 0, expired: false };
  }
}

// Secret/readiness check (doctor intentionally skipped). Capabilities encode the
// conditional rules: enrichment runs LOCALLY so Parallel/RapidAPI/OpenAI are
// always required; indexing runs on Modal (login + tokens); cloud-search keys
// (TurboPuffer/Postgres) are optional and NOT needed when Modal + local DuckDB
// cover search.
function readiness() {
  const env = readEnvSummary();
  const has = (key: string, aliases: string[] = []) =>
    Boolean((env[key] || "").trim()) || aliases.some((a) => Boolean((env[a] || "").trim()));
  const login = loginStatus();

  const secrets = [
    { key: "OPENAI_API_KEY", label: "OpenAI", provider: "OpenAI", satisfied: has("OPENAI_API_KEY"), writable: true, fix: "byo_or_pull", getUrl: "https://platform.openai.com/api-keys", optional: false },
    { key: "PARALLEL_API_KEY", label: "Parallel", provider: "Parallel", satisfied: has("PARALLEL_API_KEY"), writable: true, fix: "byo", getUrl: "https://platform.parallel.ai/settings?tab=api-keys", optional: false },
    { key: "RAPIDAPI_LINKEDIN_KEY", label: "RapidAPI (LinkedIn)", provider: "RapidAPI", satisfied: has("RAPIDAPI_LINKEDIN_KEY", ["RAPIDAPI_KEY"]), writable: true, fix: "byo", getUrl: "https://rapidapi.com/pnd-team-pnd-team/api/professional-network-data", optional: false },
    { key: "MODAL", label: "Modal tokens", provider: "Modal", satisfied: has("MODAL_TOKEN_ID") && has("MODAL_TOKEN_SECRET"), writable: false, fix: "login_pull", getUrl: "", optional: false },
    { key: "APOLLO_API_KEY", label: "Apollo", provider: "Apollo", satisfied: has("APOLLO_API_KEY"), writable: false, fix: "byo", getUrl: "https://developer.apollo.io/keys#/keys", optional: true },
    { key: "TURBOPUFFER_API_KEY", label: "TurboPuffer", provider: "TurboPuffer", satisfied: has("TURBOPUFFER_API_KEY"), writable: false, fix: "byo", getUrl: "https://turbopuffer.com", optional: true },
    { key: "DATABASE_URL", label: "Postgres", provider: "Postgres", satisfied: has("DATABASE_URL", ["SUPABASE_DATABASE_URL", "SUPABASE_DB_URL"]), writable: false, fix: "byo", getUrl: "https://supabase.com/dashboard", optional: true },
  ];

  const sat: Record<string, boolean> = { login: login.logged_in };
  for (const s of secrets) sat[s.key] = s.satisfied;

  const capabilityDefs = [
    { id: "signin", label: "Signed in to Powerset", description: "Unlocks provisioning Modal + OpenAI keys.", requires: ["login"], core: true },
    { id: "enrich", label: "Import & enrich contacts (local)", description: "Gmail / Messages / LinkedIn enrichment runs on this machine.", requires: ["OPENAI_API_KEY", "PARALLEL_API_KEY", "RAPIDAPI_LINKEDIN_KEY"], core: true },
    { id: "index", label: "Build search index (Modal cloud)", description: "Ships enriched people.csv to Modal for indexing.", requires: ["login", "MODAL"], core: true },
    { id: "outbound", label: "Apollo outbound", description: "Optional — only for Apollo-backed campaigns.", requires: ["APOLLO_API_KEY"], core: false },
    { id: "cloud_search", label: "Cloud search", description: "Optional — local DuckDB is the default; not needed when Modal + local index cover search.", requires: ["TURBOPUFFER_API_KEY"], core: false },
  ];
  const capabilities = capabilityDefs.map((def) => {
    const missing = def.requires.filter((r) => !sat[r]);
    return { ...def, satisfied: missing.length === 0, missing };
  });

  return {
    ready: capabilities.filter((c) => c.core).every((c) => c.satisfied),
    login,
    secrets,
    capabilities,
    checked_at: new Date().toISOString(),
  };
}

export async function handleSystemRoutes(req: any, res: any, url: URL): Promise<boolean> {
  if (!url.pathname.startsWith("/local-api/system/")) return false;

  if (url.pathname === "/local-api/system/readiness" && req.method === "GET") {
    sendJson(res, readiness());
    return true;
  }

  if (url.pathname === "/local-api/system/health" && req.method === "GET") {
    sendJson(res, { ok: true, ts: new Date().toISOString() });
    return true;
  }
  if (url.pathname === "/local-api/system/update-status" && req.method === "GET") {
    sendJson(res, await updateStatus());
    return true;
  }
  if (url.pathname === "/local-api/system/update" && req.method === "POST") {
    sendJson(res, startUpdate());
    return true;
  }
  if (url.pathname === "/local-api/system/daemon-status" && req.method === "GET") {
    sendJson(res, await daemonStatus());
    return true;
  }
  if (url.pathname === "/local-api/system/restart" && req.method === "POST") {
    sendJson(res, startRestart());
    return true;
  }
  return false;
}
