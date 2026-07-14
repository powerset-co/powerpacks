/** Shared types and helpers for onboarding-v2 components. */

export type JsonObject = Record<string, unknown>;

export function objectValue(value: unknown): JsonObject {
  return value && typeof value === "object" && !Array.isArray(value) ? value as JsonObject : {};
}

export function arrayValue(value: unknown): JsonObject[] {
  return Array.isArray(value) ? value.filter((item): item is JsonObject => Boolean(item && typeof item === "object" && !Array.isArray(item))) : [];
}

export function stringValue(value: unknown): string {
  return typeof value === "string" ? value : value == null ? "" : String(value);
}

export function numberValue(value: unknown): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : 0;
}

export function commandText(command: unknown): string {
  if (Array.isArray(command)) return command.map((part) => String(part)).join(" ");
  return stringValue(command);
}

export function statusTone(status: string): "default" | "secondary" | "destructive" | "outline" {
  if (status === "completed" || status === "ok" || status === "dry_run") return "default";
  if (status === "failed" || status === "blocked_approval") return "destructive";
  if (status === "running") return "secondary";
  return "outline";
}

export function selectedFileDisplayPath(file: File): string {
  const fileWithPath = file as File & { path?: string; webkitRelativePath?: string };
  const browserPath = fileWithPath.path || fileWithPath.webkitRelativePath || "";
  if (browserPath && !browserPath.includes("fakepath")) return browserPath;
  return file.name;
}

export const LINKEDIN_DEFAULT_STAGES = [
  { id: "inspect", label: "Check LinkedIn CSV" },
  { id: "discover", label: "Import LinkedIn contacts" },
  { id: "enrich", label: "Enrich LinkedIn profiles" },
  { id: "source_people", label: "Save LinkedIn people file" },
  { id: "merge_network", label: "Merge contact sources" },
  { id: "index_estimate", label: "Estimate search updates" },
  { id: "index_records", label: "Build searchable people records" },
  { id: "search_duckdb", label: "Update local search database" },
];

export const GMAIL_DEFAULT_STAGES = [
  { id: "inspect", label: "Check linked Gmail accounts" },
  { id: "discover", label: "Discover Gmail contacts" },
  { id: "enrich", label: "Enrich Gmail contacts" },
  { id: "source_people", label: "Save Gmail people file" },
  { id: "merge_network", label: "Merge contact sources" },
  { id: "index_estimate", label: "Estimate search updates" },
  { id: "index_records", label: "Build searchable people records" },
  { id: "search_duckdb", label: "Update local search database" },
];

export const DEFAULT_LINKEDIN_CSV = ".powerpacks/network-import/discover/linkedin/Connections.csv";
