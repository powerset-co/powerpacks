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

// Upsert keys into .env, preserving comments/order and other keys. Existing
// lines for a given key are rewritten in place; new keys are appended. Blank
// values are ignored so a partial save never wipes an already-set key.
export function writeEnvKeys(updates: Record<string, string>): string[] {
  const envPath = path.join(powerpacksRepoRoot, ".env");
  const clean: Record<string, string> = {};
  for (const [key, value] of Object.entries(updates)) {
    const v = (value ?? "").trim();
    if (v) clean[key] = v;
  }
  if (Object.keys(clean).length === 0) return [];

  const existing = fs.existsSync(envPath) ? fs.readFileSync(envPath, "utf8") : "";
  const remaining = { ...clean };
  const written: string[] = [];
  const lines = existing.split(/\r?\n/).map((line) => {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith("#")) return line;
    const idx = trimmed.indexOf("=");
    if (idx <= 0) return line;
    const key = trimmed.slice(0, idx).trim();
    if (Object.prototype.hasOwnProperty.call(remaining, key)) {
      const value = remaining[key];
      delete remaining[key];
      written.push(key);
      return `${key}=${value}`;
    }
    return line;
  });
  for (const [key, value] of Object.entries(remaining)) {
    lines.push(`${key}=${value}`);
    written.push(key);
  }
  let text = lines.join("\n");
  if (!text.endsWith("\n")) text += "\n";
  fs.writeFileSync(envPath, text, { mode: 0o600 });
  return written;
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
