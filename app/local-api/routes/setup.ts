import path from "path";
import fs from "fs";
import { execSync, spawnSync } from "child_process";

import {
  accountsPath,
  discoverContactsSetupLedger,
  importRefreshLedgerPath,
  messagesLedgerPath,
  messagesReviewCsvPath,
  powerpacksRepoRoot,
  powerpacksStateRoot,
  safeJoinPowerpacks,
  setupLedgerPath,
  whatsAppWacliQrHtmlPath,
  whatsAppWacliQrPngPath,
  whatsAppWahaQrPngPath,
  whatsAppWahaQrTxtPath,
} from "../lib/paths";
import { fileSummary, readJsonSync, removeLocalFiles, sha256File, writeJsonSync } from "../lib/fsUtils";
import { setupProcessEnv } from "../lib/env";
import { parseJsonFragment } from "../lib/subprocess";
import { shellJoin, shellQuote, shellStage, setupStageSummaries, stagedCommand } from "../lib/shell";
import { parseCsvDocument } from "../lib/csv";
import { accountRecords, configuredMsgvaultDb, resolveOperator, uniqueStrings } from "../lib/accounts";
import {
  SETUP_SOURCE_LABELS,
  SETUP_SOURCE_ORDER,
  clearWhatsAppLinkStatusCache,
  normalizeSetupSources,
  sourceSlug,
  whatsAppProvider,
  whatsAppQrPngRelativePath,
} from "../lib/sources";
import { messagesCurrentBlockForUi, messagesReviewResponse, resolveReviewCsvPath } from "../lib/messagesReview";
import {
  enrichmentNetworkCommand,
  gmailDiscoveryCommand,
  gmailLinkCommand,
  indexContactsCommand,
  linkedinDiscoveryCommand,
  messagesDiscoveryCommand,
  msgvaultHomeArgs,
  setupCommandArgs,
  wahaRuntimeCommand,
  wahaSessionCommand,
  wahaWaitTimeoutSeconds,
} from "../lib/commands";
import { clearLocalDuckdbTableCountsCache, localDuckdbTableCounts } from "../lib/duckdb";
import { readRequestJson, sendBinary, sendJson } from "../lib/http";
import { setupJobs, setupJobsList, startSetupJob, startWhitelistedShellJob } from "../jobs";
import type { RunState, SetupJob, SetupOperator } from "../lib/types";

// Pre-existing latent reference: messagesImportLedgerStatus (currently unused)
// reads this constant, which was never defined in the original plugin either.
declare const messagesContactsCsvPath: string;

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
type SetupStatusTab = "link" | "discover" | "enrichment" | "index" | "all";

let cachedIndexEstimate: { key: string; expiresAt: number; value: Record<string, any> } | null = null;
let cachedIndexCoverage: { key: string; expiresAt: number; value: Record<string, any> } | null = null;

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
  clearLocalDuckdbTableCountsCache();
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
  const localRecords = includeIndex ? localSearchRecordSummary() : { recordFiles: 0, nonemptyRecordFiles: 0 };
  const localRecordsPreferred = Boolean(localRecords.nonemptyRecordFiles)
    && (!duckdbFile.exists || !duckdbHasRows);
  const processingEstimate = !includeIndex ? {
    status: "not_loaded",
    totalEstimatedUsd: 0,
    estimatedPaidCalls: {},
    counts: {},
    providers: {},
    error: "",
  } : localRecordsPreferred ? {
    status: "local_records_restore",
    totalEstimatedUsd: 0,
    estimatedPaidCalls: {},
    counts: { records: Number(localRecords.nonemptyRecordFiles || 0) },
    providers: {},
    error: "",
  } : lastIndexProcessingEstimate(peopleSha256);
  const indexCoverage = includeIndex ? localIndexCoverage(peopleSha256, duckdbPath) : { status: "not_loaded", totalPeople: 0, indexedPeople: 0, pendingPeople: 0 };
  const importLiveRefresh = phases.import?.live_refresh || importRefreshLedger.refresh || importRefreshLedger;
  const messagesCurrentBlock = messagesCurrentBlockForUi(messagesLedger, reviewApi.counts || {});

  return {
    operator,
    setup: {
      ...setupFile,
      status: setupLedger.status || "unknown",
      updatedAt: setupLedger.updated_at || setupFile.updatedAt || null,
      phases: {
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
      localRecords,
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

  if (["link", "run"].includes(action)) {
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
    const force = body.force === true;
    const source = buildImportSources(accounts, operator.id).find((candidate) => candidate.id === sourceId);
    if (!source) throw new Error(`unsupported import source: ${sourceId}`);
    if (!source.linked) throw new Error(`source is not linked: ${sourceId}`);
    if (source.skipped) throw new Error(`source is skipped: ${sourceId}`);
    if (source.runnable === false || source.command.length === 0) {
      throw new Error(source.disabledReason || `source is not importable yet: ${sourceId}`);
    }
    const command = enrichmentNetworkCommand(operator.id, sourceId, { approveSpend, force });
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
    // Directly record a discovered msgvault account in accounts.json.
    // These accounts are already authorized in msgvault so we just need
    // to persist them — no browser flow, no onboarding step machinery.
    const email = requireString(body.email, "email").trim().toLowerCase();
    const accounts = readJsonSync(accountsPath) || {};
    const records = accountRecords(accounts);
    const gmail = records.gmail || {};
    const config = gmail.config && typeof gmail.config === "object" ? gmail.config : {};
    const dbPath = configuredMsgvaultDb(accounts);
    const prev = {
      account_emails: Array.isArray(config.account_emails) ? config.account_emails as string[] : [],
      selected_accounts: Array.isArray(config.selected_accounts) ? config.selected_accounts as string[] : [],
    };
    const account_emails = uniqueStrings([...prev.account_emails, email]);
    const selected_accounts = uniqueStrings([...prev.selected_accounts, email]);
    const now = new Date().toISOString();
    const next = {
      ...accounts,
      accounts: {
        ...records,
        gmail: {
          ...gmail,
          linked: true,
          skipped: false,
          usernames: selected_accounts,
          artifacts: Array.isArray(gmail.artifacts) ? gmail.artifacts : [],
          config: { ...config, msgvault_db: dbPath, account_emails, selected_accounts, pending_accounts: [] },
          last_checked_at: now,
          last_success_at: now,
          notes: "Linked from onboarding v2 UI.",
        },
      },
      updated_at: now,
    };
    writeJsonSync(accountsPath, next);
    return startSetupJob(action, ["echo", JSON.stringify({ status: "ok", email, linked: true })], 5000);
  }

  if (action === "gmail-reauth") {
    const email = requireString(body.email, "email");
    const homeArgs = msgvaultHomeArgs();
    return startSetupJob(action, [
      "msgvault", "add-account", email, "--force", ...homeArgs,
    ], 5 * 60 * 1000);
  }

  if (action === "gmail-all") {
    return startSetupJob(action, setupCommandArgs(operator.id, "link", ["--gmail-all"]));
  }

  if (action === "gmail-link-emails") {
    return startSetupJob(action, gmailLinkCommand(operator.id, body.emails, { skipAuthorize: Boolean(body.skipAuthorize) }), 60 * 60 * 1000);
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
    clearWhatsAppLinkStatusCache();
    // Clean stale wacli lock if the holding process is dead
    const wacliStore = path.resolve(powerpacksRepoRoot, ".powerpacks/messages/wacli");
    const wacliLock = path.join(wacliStore, "LOCK");
    if (fs.existsSync(wacliLock)) {
      try {
        const lockContent = fs.readFileSync(wacliLock, "utf8");
        const pidMatch = lockContent.match(/pid=(\d+)/);
        if (pidMatch) {
          const lockPid = Number(pidMatch[1]);
          try {
            process.kill(lockPid, 0); // just checks if alive
          } catch {
            // Process is dead — remove stale lock
            fs.unlinkSync(wacliLock);
          }
        }
      } catch { /* ignore */ }
    }
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
    // Kill any lingering wacli processes before starting fresh auth
    try {
      execSync("pkill -f 'wacli.*--store.*wacli' 2>/dev/null || true", { timeout: 5000 });
    } catch { /* ignore */ }
    removeLocalFiles([whatsAppWacliQrPngPath, whatsAppWacliQrHtmlPath]);
    // Run auth, then probe doctor and write status back to accounts.json
    const authCmd = shellJoin([
      "uv", "run", "--project", ".", "python",
      "packs/messages/primitives/import_whatsapp_wacli/import_whatsapp_wacli.py",
      "auth", "--store", ".powerpacks/messages/wacli", "--no-open-qr-page",
    ]);
    const writeBackCmd = shellJoin([
      "uv", "run", "--project", ".", "python", "-c",
      [
        "import json, subprocess, pathlib;",
        "p=pathlib.Path('.powerpacks/ingestion/accounts.json');",
        "d=json.loads(p.read_text()) if p.exists() else {'accounts':{},'version':2};",
        "r=subprocess.run(['wacli','--store','.powerpacks/messages/wacli','doctor','--json'],capture_output=True,text=True,timeout=5);",
        "ok=(json.loads(r.stdout).get('data',{}).get('authenticated') if r.stdout else False);",
        "m=d.setdefault('accounts',{}).setdefault('messages',{});",
        "c=m.setdefault('config',{});",
        "c.setdefault('whatsapp',{}).update({'authenticated':bool(ok),'status':'authenticated' if ok else 'not_authenticated'});",
        "m['linked']=bool(ok or c.get('imessage',{}).get('readable'));",
        "p.write_text(json.dumps(d,indent=2)+'\\n');",
        "print(json.dumps({'whatsapp_authenticated':bool(ok)}))",
      ].join(""),
    ]);
    // Also clean QR files after successful auth
    const cleanQr = `rm -f ${shellQuote(whatsAppWacliQrPngPath)} ${shellQuote(whatsAppWacliQrHtmlPath)} 2>/dev/null || true`;
    return startSetupJob(action, [
      "/bin/zsh", "-lc", `${authCmd} && ${writeBackCmd} && ${cleanQr}`,
    ], 10 * 60 * 1000);
  }

  if (action === "open-message-permissions") {
    const openCmd = shellJoin([
      "uv", "run", "--project", ".", "python",
      "packs/messages/primitives/extract_imessage_contacts/extract_imessage_contacts.py",
      "open-privacy-settings", "--target", "both",
    ]);
    const writeBackImessage = shellJoin([
      "uv", "run", "--project", ".", "python", "-c",
      [
        "import json, os, pathlib;",
        "p=pathlib.Path('.powerpacks/ingestion/accounts.json');",
        "d=json.loads(p.read_text()) if p.exists() else {'accounts':{},'version':2};",
        "chat_db=os.path.expanduser('~/Library/Messages/chat.db');",
        "readable=os.access(chat_db, os.R_OK);",
        "m=d.setdefault('accounts',{}).setdefault('messages',{});",
        "c=m.setdefault('config',{});",
        "c['imessage']={'readable':readable,'status':'ready' if readable else 'not_ready','chat_db':chat_db};",
        "m['linked']=bool(readable or c.get('whatsapp',{}).get('authenticated'));",
        "p.write_text(json.dumps(d,indent=2)+'\\n');",
        "print(json.dumps({'imessage_readable':readable}))",
      ].join(""),
    ]);
    return startSetupJob(action, [
      "/bin/zsh", "-lc", `${openCmd}; sleep 2; ${writeBackImessage}`,
    ], 2 * 60 * 1000);
  }

  if (action === "twitter-handle") {
    return startSetupJob(action, setupCommandArgs(operator.id, "link", ["--twitter-handle", requireString(body.handle, "handle")]));
  }

  throw new Error(`unsupported setup action: ${action}`);
}

export async function handleSetupRoutes(req: any, res: any, url: URL): Promise<boolean> {
  if (url.pathname === "/local-api/profile") {
    sendJson(res, localProfile());
    return true;
  }

  if (url.pathname === "/local-api/setup/status") {
    sendJson(res, await setupStatus(url.searchParams.get("tab") || url.searchParams.get("scope")));
    return true;
  }

  if (url.pathname === "/local-api/setup/whatsapp-qr") {
    const relativePath = String(url.searchParams.get("path") || whatsAppQrPngRelativePath());
    const resolved = safeJoinPowerpacks(relativePath);
    const messagesDir = `${path.join(powerpacksStateRoot, "messages")}${path.sep}`;
    if (!resolved || !resolved.startsWith(messagesDir) || path.extname(resolved).toLowerCase() !== ".png" || !fs.existsSync(resolved)) {
      sendJson(res, { error: "WhatsApp QR not found" }, 404);
      return true;
    }
    sendBinary(res, fs.readFileSync(resolved), "image/png");
    return true;
  }

  if (url.pathname === "/local-api/setup/jobs") {
    sendJson(res, { jobs: setupJobsList() });
    return true;
  }

  if (url.pathname === "/local-api/setup/linkedin-csv-upload" && req.method === "POST") {
    sendJson(res, saveLinkedInCsvUpload(await readRequestJson(req)));
    return true;
  }

  const setupJobMatch = url.pathname.match(/^\/local-api\/setup\/jobs\/([^/]+)$/);
  if (setupJobMatch) {
    const job = setupJobs.get(decodeURIComponent(setupJobMatch[1]));
    if (job) sendJson(res, job);
    else sendJson(res, { error: "Setup job not found" }, 404);
    return true;
  }

  if (url.pathname === "/local-api/setup/run" && req.method === "POST") {
    const body = await readRequestJson(req);
    const job = buildSetupActionJob(body);
    sendJson(res, { job });
    return true;
  }

  return false;
}
