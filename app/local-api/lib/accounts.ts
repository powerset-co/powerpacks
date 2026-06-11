import path from "path";
import fs from "fs";
import { spawnSync } from "child_process";

import { accountsPath, powerpacksRepoRoot, powerpacksStateRoot } from "./paths";
import { readJsonSync, writeJsonSync } from "./fsUtils";
import { readEnvSummary, setupProcessEnv } from "./env";
import { parseJsonFragment } from "./subprocess";
import type { RunState, SetupOperator } from "./types";

export function accountRecords(accounts: RunState | null): Record<string, any> {
  const records = accounts?.accounts || accounts?.channels || accounts?.sources || {};
  return records && typeof records === "object" ? records : {};
}

export function uniqueStrings(values: unknown[]): string[] {
  return [...new Set(values.map((value) => String(value || "").trim().toLowerCase()).filter(Boolean))];
}

export function configuredMsgvaultDb(accounts: RunState | null): string {
  const gmail = accountRecords(accounts).gmail || {};
  const config = gmail.config && typeof gmail.config === "object" ? gmail.config : {};
  const configured = String(config.msgvault_db || "").trim();
  if (configured) return path.resolve(configured.replace(/^~(?=\/|$)/, process.env.HOME || ""));
  const home = process.env.MSGVAULT_HOME ? path.resolve(process.env.MSGVAULT_HOME) : path.join(process.env.HOME || "", ".msgvault");
  return path.join(home, "msgvault.db");
}

export function localGmailAccountsFromRecord(record: Record<string, any>): string[] {
  const config = record.config && typeof record.config === "object" ? record.config : {};
  return uniqueStrings([
    ...((Array.isArray(config.selected_accounts) ? config.selected_accounts : []) as unknown[]),
    ...((Array.isArray(config.account_emails) ? config.account_emails : []) as unknown[]),
    ...((Array.isArray(record.usernames) ? record.usernames : []) as unknown[]),
  ]);
}

export function shouldAutoLinkGmailRecord(record: Record<string, any>): boolean {
  if (localGmailAccountsFromRecord(record).length > 0) return false;
  if (record.linked === true && record.skipped !== true) return false;
  if (record.skipped !== true) return true;
  const notes = String(record.notes || "").toLowerCase();
  return notes.includes("bootstrap") || notes.includes("local search pipeline");
}

export function discoverMsgvaultAccounts(dbPath: string): { accounts: string[]; rows: Record<string, any>[]; error?: string } {
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

export function autoLinkGmailFromMsgvault(accounts: RunState | null): RunState {
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

export function resolveOperator(setupLedger: RunState | null, accounts: RunState | null): SetupOperator {
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
