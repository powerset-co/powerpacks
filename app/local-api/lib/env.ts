import path from "path";
import fs from "fs";

import { powerpacksRepoRoot } from "./paths";

export function readEnvSummary(): Record<string, string> {
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

export function setupProcessEnv(): NodeJS.ProcessEnv {
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
