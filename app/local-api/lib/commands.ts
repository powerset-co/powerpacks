import path from "path";
import fs from "fs";
import { createHash } from "crypto";

import {
  discoverContactsSetupLedger,
  importRefreshLedgerPath,
  setupLedgerPath,
  whatsAppWahaEngine,
  whatsAppWahaImage,
} from "./paths";
import { readJsonSync } from "./fsUtils";
import { setupProcessEnv } from "./env";
import { shellJoin, shellQuote, shellStage } from "./shell";
import { resolveOperator } from "./accounts";
import { normalizeSetupSources } from "./sources";
import type { RunState } from "./types";

export function setupCommandArgs(operatorId: string, phase: "status" | "next" | "link" | "import" | "fan-in" | "index" | "run", extra: string[] = []) {
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

export function onboardingV2LinkedInCommand(command: "dry-run" | "run", operatorId: string, options: { csvPath?: string; sourceLabel?: string; force?: boolean } = {}) {
  const args = [
    "uv", "run", "--project", ".", "python",
    "packs/ingestion/primitives/setup_linkedin_csv/setup_linkedin_csv.py",
    command,
    "--operator-id", operatorId,
    "--accounts", ".powerpacks/ingestion/accounts.json",
  ];
  if (options.csvPath) args.push("--csv", options.csvPath);
  if (options.sourceLabel) args.push("--source-user", options.sourceLabel);
  if (options.force) args.push("--force");
  return args;
}

export function onboardingV3PipelineCommand(options: { csvPath: string; sourceLabel?: string; force?: boolean }) {
  const args = [
    "uv", "run", "--project", ".", "python",
    "packs/indexing/modal/linkedin_modal_pipeline.py",
    "pipeline",
    "--csv", options.csvPath,
  ];
  if (options.sourceLabel) args.push("--source-user", options.sourceLabel);
  if (options.force) args.push("--force");
  return args;
}

export function normalizeEmailList(value: unknown): string[] {
  const values = Array.isArray(value)
    ? value.flatMap((item) => String(item).split(/[,\n\s]+/))
    : String(value || "").split(/[,\n\s]+/);
  return [...new Set(values.map((item) => item.trim().toLowerCase()).filter(Boolean))];
}

export function gmailOauthProjectId(email: string): string {
  const digest = createHash("sha1").update(email.trim().toLowerCase()).digest("hex").slice(0, 14);
  return `local-msg-vault-${digest}`;
}

export function msgvaultHomeArgs(): string[] {
  const configured = process.env.MSGVAULT_HOME ? path.resolve(process.env.MSGVAULT_HOME) : "";
  const defaultHome = path.join(process.env.HOME || "", ".msgvault");
  return configured && configured !== defaultHome ? ["--home", configured] : [];
}

export function msgvaultOauthConfigured(): boolean {
  const home = process.env.MSGVAULT_HOME ? path.resolve(process.env.MSGVAULT_HOME) : path.join(process.env.HOME || "", ".msgvault");
  const config = path.join(home, "config.toml");
  try {
    return fs.readFileSync(config, "utf8").includes("client_secrets");
  } catch {
    return false;
  }
}

export function gmailLinkCommand(
  operatorId: string,
  rawEmails: unknown,
  opts: { skipTestUsers?: boolean; skipAuthorize?: boolean } = {},
): string[] {
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
    // skipTestUsers: the emails are already OAuth test users (added at create
    // time), so the Authorize action skips the add-test-users browser step (no
    // GCP console panel) and just registers + grants.
    if (!opts.skipTestUsers) {
      automation.push([
        "uv", "run", "--project", ".", "python",
        "packs/ingestion/primitives/msgvault_setup/msgvault_setup.py",
        "add-test-users",
        ...emails,
        ...homeArgs,
      ]);
    }
    // skipAuthorize: Add only registers + adds the test user and leaves the
    // account PENDING — no per-account grant. The user authorizes separately.
    if (!opts.skipAuthorize) {
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
  }

  const recordPending = `${shellJoin(setupAdd)}; code=$?; if [[ $code -ne 0 && $code -ne 20 ]]; then exit $code; fi`;
  // Only mark authorized in accounts.json when we actually granted (add-account).
  const parts = [recordPending, ...automation.map(shellJoin)];
  if (!opts.skipAuthorize) parts.push(shellJoin(setupAuthorized));
  return ["/bin/zsh", "-lc", parts.join(" && ")];
}

export function wahaRuntimeCommand(command: "up" | "status", extra: string[] = []): string[] {
  return [
    "uv", "run", "--project", ".", "python",
    "packs/messages/primitives/waha_runtime/waha_runtime.py",
    command,
    "--engine", whatsAppWahaEngine,
    "--image", whatsAppWahaImage,
    ...extra,
  ];
}

export function wahaSessionCommand(command: "start" | "status", extra: string[] = []): string[] {
  return [
    "uv", "run", "--project", ".", "python",
    "packs/messages/primitives/waha_session/waha_session.py",
    command,
    "--engine", whatsAppWahaEngine,
    ...extra,
  ];
}

export function wahaWaitTimeoutSeconds(): string {
  const raw = String(setupProcessEnv().POWERPACKS_WAHA_WAIT_TIMEOUT || "180").trim();
  const seconds = Number.parseInt(raw, 10);
  return Number.isFinite(seconds) && seconds > 0 ? String(seconds) : "180";
}

export function discoverContactsCommand(operatorId: string, extra: string[] = []) {
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

export function gmailDiscoveryCommand(): string[] {
  return [
    "uv", "run", "--project", ".", "python",
    "packs/ingestion/primitives/discover_contacts_pipeline/gmail.py",
    "discover",
    "--accounts", ".powerpacks/ingestion/accounts.json",
  ];
}

export function linkedinDiscoveryCommand(): string[] {
  return [
    "uv", "run", "--project", ".", "python",
    "packs/ingestion/primitives/discover_contacts_pipeline/linkedin.py",
    "discover",
    "--accounts", ".powerpacks/ingestion/accounts.json",
  ];
}

export function messagesDiscoveryCommand(): string[] {
  return [
    "uv", "run", "--project", ".", "python",
    "packs/ingestion/primitives/discover_contacts_pipeline/messages.py",
    "discover",
    "--accounts", ".powerpacks/ingestion/accounts.json",
  ];
}

export function enrichmentNetworkCommand(operatorId: string, sourceId: string, options: { approveSpend?: boolean; force?: boolean } = {}): string[] {
  const source = sourceId === "linkedin_csv" ? "linkedin" : sourceId;
  if (!["gmail", "linkedin", "messages"].includes(source)) return [];
  const command = [
    "uv", "run", "--project", ".", "python",
    `packs/ingestion/primitives/import_contacts_pipeline/${source}.py`,
    "run",
    "--accounts", ".powerpacks/ingestion/accounts.json",
    "--operator-id", operatorId,
  ];
  // Spend flags are per-source: messages = deep research (--confirm-import),
  // gmail = Parallel.ai (--approve-parallel-spend). LinkedIn is RapidAPI (free)
  // and its primitive accepts neither flag, so it gets none.
  if (options.approveSpend && source === "messages") command.push("--confirm-import");
  else if (options.approveSpend && source === "gmail") command.push("--approve-parallel-spend");
  // Force a real re-run so Sync/Import never no-ops on an unchanged manifest.
  // messages has its own ledger/resume, so --force only applies to gmail/linkedin.
  if (options.force && source !== "messages") command.push("--force");
  return command;
}

// Gmail "Process": enrich locally (Parallel.ai email+context), refresh the
// canonical merged people.csv, then ship it to Modal for index-only. Chained in
// one shell so a single job/status covers the button flow.
export function onboardingGmailRunCommand(operatorId: string): string[] {
  const enrich = enrichmentNetworkCommand(operatorId, "gmail", { approveSpend: true, force: true });
  const fanIn = [
    "uv", "run", "--project", ".", "python",
    "packs/indexing/primitives/index_contacts_pipeline/index_contacts_pipeline.py",
    "fan-in",
    "--operator-id", operatorId,
    "--accounts", ".powerpacks/ingestion/accounts.json",
    "--people-csv", ".powerpacks/network-import/merged/people.csv",
    "--no-include-existing-artifacts",
  ];
  const index = [
    "uv", "run", "--project", ".", "python",
    "packs/indexing/modal/linkedin_modal_pipeline.py",
    "index-people",
    "--people-csv", ".powerpacks/network-import/merged/people.csv",
  ];
  return ["bash", "-c", [enrich, fanIn, index].map(shellJoin).join(" && ")];
}

// Free + instant: reads the resolution queue minus directory.csv to estimate the
// incremental Parallel.ai spend for the next Gmail Process. No API calls.
export function gmailEnrichEstimateCommand(): string[] {
  return [
    "uv", "run", "--project", ".", "python",
    "packs/ingestion/primitives/setup_gmail/setup_gmail.py",
    "estimate",
  ];
}

export function processLocalNetworkCommand(operatorId: string): string[] {
  return [
    "uv", "run", "--project", ".", "python",
    "packs/indexing/primitives/index_contacts_pipeline/index_contacts_pipeline.py",
    "fan-in",
    "--operator-id", operatorId,
    "--accounts", ".powerpacks/ingestion/accounts.json",
  ];
}

export function indexContactsCommand(operatorId: string, extra: string[] = []): string[] {
  return [
    "uv", "run", "--project", ".", "python",
    "packs/indexing/primitives/index_contacts_pipeline/index_contacts_pipeline.py",
    "run",
    "--operator-id", operatorId,
    "--accounts", ".powerpacks/ingestion/accounts.json",
    ...extra,
  ];
}

export function messageImportCommand(source: ReturnType<typeof normalizeSetupSources>[number]) {
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

export function messageEnrichmentCommand(accounts: RunState | null): string[] {
  return enrichmentNetworkCommand(resolveOperator(readJsonSync(setupLedgerPath) || {}, accounts).id, "messages");
}

export function importAndFanInCommand(importCommand: string[], fanInCommand: string[], label: string): string[] {
  return importsAndFanInCommand([{ label, command: importCommand }], fanInCommand);
}

export function importsAndFanInCommand(imports: Array<{ label: string; command: string[] }>, fanInCommand: string[]): string[] {
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
