import { spawn } from "child_process";
import { randomUUID } from "crypto";

import { powerpacksRepoRoot } from "./lib/paths";
import { setupProcessEnv } from "./lib/env";
import { parseLastJsonFragment } from "./lib/subprocess";
import type { SetupJob } from "./lib/types";

export const setupJobs = new Map<string, SetupJob>();

function pruneSetupJobs() {
  const jobs = [...setupJobs.values()].sort((a, b) => b.startedAt.localeCompare(a.startedAt));
  for (const job of jobs.slice(40)) setupJobs.delete(job.id);
}

export function startSetupJob(action: string, command: string[], timeoutMs = 6 * 60 * 60 * 1000, metadata: Pick<SetupJob, "actionKey" | "source" | "stages"> = {}): SetupJob {
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

export function startWhitelistedShellJob(command: string): SetupJob {
  if (!whitelistedShellCommand(command)) {
    throw new Error("Command is not allowed from the local setup UI");
  }
  return startSetupJob("run-command", ["/bin/zsh", "-lc", command]);
}

export function setupJobsList(): SetupJob[] {
  return [...setupJobs.values()].sort((a, b) => b.startedAt.localeCompare(a.startedAt));
}
