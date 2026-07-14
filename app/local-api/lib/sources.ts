import { accountRecords } from "./accounts";
import type { RunState } from "./types";

export const SETUP_SOURCE_ORDER = ["gmail", "linkedin_csv", "messages", "twitter"] as const;
export const SETUP_SOURCE_LABELS: Record<string, string> = {
  gmail: "Gmail",
  linkedin_csv: "LinkedIn",
  messages: "Messages",
  twitter: "Twitter/X",
};

export function normalizeSetupSources(accounts: RunState | null) {
  const records = accountRecords(accounts);
  return SETUP_SOURCE_ORDER.map((id) => {
    const record = records[id] || {};
    const linked = Boolean(record.linked || record.status === "linked");
    const skipped = Boolean(record.skipped || record.status === "skipped");
    const config = record.config && typeof record.config === "object" ? record.config : {};
    return {
      id,
      label: SETUP_SOURCE_LABELS[id],
      status: skipped ? "skipped" : linked ? "linked" : record.status || "unlinked",
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

export function sourceSlug(value: string): string {
  return (value || "source").toLowerCase().replace(/[^a-z0-9._-]+/g, "-").replace(/^[._-]+|[._-]+$/g, "") || "source";
}
