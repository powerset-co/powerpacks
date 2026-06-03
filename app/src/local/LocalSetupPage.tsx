import { useCallback, useEffect, useMemo, useState } from "react";
import type { ComponentProps, ReactNode } from "react";
import { formatDistanceToNow } from "date-fns";
import {
  AtSign,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  CircleAlert,
  CircleDot,
  CircleSlash,
  Database,
  FileText,
  HardDrive,
  Link2,
  Loader2,
  Mail,
  MessageSquare,
  Play,
  Sparkles,
  Terminal,
} from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";
import {
  fetchSetupStatus,
  runSetupAction,
  uploadLinkedInCsv,
} from "./powerpacksApi";
import type {
  SetupEnrichmentSource,
  SetupImportSource,
  SetupJob,
  SetupSourceId,
  SetupSourceStatus,
  SetupStatusResponse,
} from "./types";

type SetupTabId = "link" | "import" | "enrichment" | "index";
type LinkRowId = "gmail" | "linkedin_csv" | "imessage" | "whatsapp" | "twitter";
type SetupStepState = "complete" | "current" | "blocked" | "upcoming";

const TABS: Array<{ id: SetupTabId; label: string; shortLabel: string; icon: typeof Link2; description: string; action: string }> = [
  { id: "link", label: "Link accounts", shortLabel: "Link", icon: Link2, description: "Connect the sources you want to use. Skip anything you do not need.", action: "Link or skip each source" },
  { id: "import", label: "Import data", shortLabel: "Import", icon: Database, description: "Pull fresh local metadata from linked sources.", action: "Run Full Import" },
  { id: "enrichment", label: "Enrich people", shortLabel: "Enrich", icon: Sparkles, description: "Resolve profiles and review researched people before adding them.", action: "Run enrichment" },
  { id: "index", label: "Process index", shortLabel: "Process", icon: HardDrive, description: "Merge source-specific people and build the searchable local index.", action: "Build index" },
];
const TAB_IDS = new Set(TABS.map((tab) => tab.id));

const SOURCE_ICONS: Record<SetupSourceId, typeof Mail> = {
  gmail: Mail,
  linkedin_csv: FileText,
  messages: MessageSquare,
  twitter: AtSign,
};

function phaseTone(status?: string): "default" | "secondary" | "outline" | "destructive" {
  const normalized = String(status || "").toLowerCase();
  if (["authenticated", "ready", "completed", "restored", "linked", "source_import_completed", "source_enrichment_completed", "selected_steps_completed"].includes(normalized)) return "default";
  if (["not_authenticated", "skipped", "pending", "unknown", "not_ready"].includes(normalized)) return "secondary";
  if (["permission_required"].includes(normalized) || normalized.startsWith("blocked") || normalized === "failed") return "destructive";
  return "outline";
}

function statusDisplayLabel(status?: string): string {
  const normalized = String(status || "unknown").toLowerCase();
  const labels: Record<string, string> = {
    blocked_user_action: "action needed",
    blocked_approval: "approval needed",
    authenticated: "authenticated",
    needs_agent_action: "action needed",
    needs_input: "needs input",
    not_authenticated: "not authenticated",
    not_linked: "available",
    not_ready: "not ready",
    permission_required: "permission required",
    pending: "not started",
    refresh_due: "ready to import",
    selected_steps_completed: "completed",
    source_enrichment_completed: "completed",
    source_import_completed: "completed",
    unlinked: "available",
  };
  return (labels[normalized] || normalized).replace(/_/g, " ");
}

function StatusBadge({ status }: { status?: string }) {
  return (
    <Badge variant={phaseTone(status)} className="capitalize">
      {statusDisplayLabel(status)}
    </Badge>
  );
}

function SourceIcon({ source }: { source: SetupSourceStatus }) {
  const Icon = SOURCE_ICONS[source.id] || CircleDot;
  return (
    <div className="flex h-9 w-9 items-center justify-center rounded-md border bg-background">
      <Icon className="h-4 w-4 text-muted-foreground" />
    </div>
  );
}

function SourceStateIcon({ source }: { source: SetupSourceStatus }) {
  if (source.linked) return <CheckCircle2 className="h-4 w-4 text-emerald-600" />;
  if (source.skipped) return <CircleSlash className="h-4 w-4 text-muted-foreground" />;
  return <CircleAlert className="h-4 w-4 text-amber-600" />;
}

function stringList(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.map(String).filter(Boolean);
}

function stringValue(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function boolValue(value: unknown): boolean {
  return value === true || value === "true" || value === 1 || value === "1";
}

function sourceConfig(source?: SetupSourceStatus | null): Record<string, unknown> {
  return source?.config && typeof source.config === "object" ? source.config : {};
}

function updatedLabel(value?: string | null): string {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return formatDistanceToNow(date, { addSuffix: true });
}

function refreshLabel(status: SetupStatusResponse): string {
  const value = status.import.updatedAt || status.setup.updatedAt;
  const label = updatedLabel(value);
  if (!label) return "";
  return `Last refreshed ${label}`;
}

function hasSetupTabParam(): boolean {
  return new URLSearchParams(window.location.search).has("tab");
}

function setupTabFromLocation(): SetupTabId {
  const tab = new URLSearchParams(window.location.search).get("tab") || "";
  return TAB_IDS.has(tab as SetupTabId) ? tab as SetupTabId : "link";
}

function setSetupTabParam(tab: SetupTabId) {
  const url = new URL(window.location.href);
  url.pathname = "/setup";
  url.searchParams.set("tab", tab);
  const next = `${url.pathname}?${url.searchParams.toString()}`;
  if (`${window.location.pathname}${window.location.search}` !== next) {
    window.history.pushState({}, "", next);
  }
}

function cleanJobText(value?: string): string {
  return String(value || "").replace(/\.powerpacks\/[^\s",}]+\.json/g, "local state file");
}

function latestJobLine(job: SetupJob): string {
  return cleanJobText(job.log || [job.stdout, job.stderr].filter(Boolean).join("\n"))
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean)
    .slice(-1)[0] || "";
}

function jobSummary(job: SetupJob): string {
  const output = job.output || {};
  const payload = (output.payload || output) as Record<string, unknown>;
  const message = cleanJobText(
    stringValue(payload.message)
      || stringValue(payload.reason)
      || stringValue(output.message)
      || stringValue(output.reason)
  );
  if (message) return message;
  if (job.status === "running") {
    const line = latestJobLine(job);
    if (line) return line;
  }
  if (job.status === "running") return `Started ${updatedLabel(job.startedAt) || "now"}`;
  if (job.completedAt) return `Finished ${updatedLabel(job.completedAt)}`;
  if (typeof job.code === "number") return `Exited with code ${job.code}`;
  return "No output yet.";
}

interface ExtractedCommand {
  label: string;
  command: string;
  description?: string;
}

function normalizedCommandLabel(label: string): string {
  return label.toLowerCase().replace(/[_-]+/g, " ").replace(/\s+/g, " ").trim();
}

function commandSubject(label: string): string {
  const normalized = normalizedCommandLabel(label);
  return normalized.replace(/^(run|show|view|status|check)\s+/, "").trim() || normalized;
}

function isDisplayOnlyCommand(command: ExtractedCommand): boolean {
  return /^(show|view|status|check)\b/.test(normalizedCommandLabel(command.label));
}

function collapseDisplayCommandDuplicates(commands: ExtractedCommand[]): ExtractedCommand[] {
  const runnableSubjects = new Set(
    commands
      .filter((command) => !isDisplayOnlyCommand(command))
      .map((command) => commandSubject(command.label))
      .filter(Boolean),
  );
  const seenCommands = new Set<string>();
  return commands.filter((command) => {
    if (isDisplayOnlyCommand(command) && runnableSubjects.has(commandSubject(command.label))) return false;
    const key = command.command.trim();
    if (seenCommands.has(key)) return false;
    seenCommands.add(key);
    return true;
  });
}

function extractCommands(job?: SetupJob | null): ExtractedCommand[] {
  const output = job?.output || {};
  const payload = (output.payload || output) as Record<string, any>;
  const hideLinkOnlyFollowups = job?.action === "gmail-link-emails";
  const candidates = [
    ...(Array.isArray(output.commands) ? output.commands : []),
    ...(Array.isArray(payload.commands) ? payload.commands : []),
  ];
  for (const key of ["repeat_command_after_authorization", "repeat_command", "next_command", "skip_command", "skip_whatsapp_command"]) {
    if (hideLinkOnlyFollowups && ["repeat_command_after_authorization", "repeat_command", "next_command"].includes(key)) continue;
    const value = payload[key] || output[key];
    if (typeof value === "string" && value.trim()) {
      candidates.push({ label: key.replace(/_/g, " "), command: value });
    }
  }
  return collapseDisplayCommandDuplicates(candidates
    .map((command: any) => ({
      label: String(command.label || "run command"),
      command: String(command.command || ""),
      description: command.description ? String(command.description) : undefined,
    }))
    .filter((command) => command.command)
    .filter((command) => !hideLinkOnlyFollowups || !/rerun[_ ]onboarding|repeat command|next command/i.test(command.label)));
}

function isCompleteStatus(status?: string): boolean {
  const normalized = String(status || "").toLowerCase();
  return ["ready", "completed", "restored", "source_import_completed", "source_enrichment_completed", "selected_steps_completed"].includes(normalized);
}

function isBlockedStatus(status?: string): boolean {
  const normalized = String(status || "").toLowerCase();
  return normalized.startsWith("blocked") || normalized === "failed" || normalized === "permission_required";
}

function setupStepProgress(status: SetupStatusResponse, active: SetupTabId) {
  const phases = status.setup.phases || {};
  const sourceRows = status.enrichment.sources || [];
  const enrichmentComplete = isCompleteStatus(status.enrichment.status)
    || sourceRows.some((source) => isCompleteStatus(source.status))
    || Number(status.enrichment.totalEnriched || 0) > 0;
  const indexComplete = isCompleteStatus(phases.index)
    || String(status.index.readiness || "").toLowerCase() === "ready"
    || Boolean(status.index.duckdbExists && status.index.peopleSha256 && status.index.indexInputSha256 === status.index.peopleSha256);
  const completeByStep: Record<SetupTabId, boolean> = {
    link: isCompleteStatus(phases.link) || status.accounts.unresolvedSources.length === 0,
    import: isCompleteStatus(phases.import) || isCompleteStatus(status.import.status),
    enrichment: enrichmentComplete,
    index: indexComplete,
  };
  const blockedByStep: Record<SetupTabId, boolean> = {
    link: isBlockedStatus(phases.link),
    import: isBlockedStatus(phases.import) || isBlockedStatus(status.import.status),
    enrichment: isBlockedStatus(status.enrichment.status) || sourceRows.some((source) => Boolean(source.blocked)),
    index: isBlockedStatus(phases.index),
  };
  const recommended = TABS.find((tab) => !completeByStep[tab.id])?.id || "index";
  return TABS.map((tab, index) => ({
    ...tab,
    index,
    complete: completeByStep[tab.id],
    blocked: blockedByStep[tab.id],
    recommended: tab.id === recommended,
    state: blockedByStep[tab.id] ? "blocked" : completeByStep[tab.id] ? "complete" : tab.id === active ? "current" : "upcoming" as SetupStepState,
  }));
}

function SetupStepper({
  active,
  status,
  onChange,
}: {
  active: SetupTabId;
  status: SetupStatusResponse;
  onChange: (tab: SetupTabId) => void;
}) {
  const steps = setupStepProgress(status, active);
  const current = steps.find((step) => step.id === active) || steps[0];
  const recommended = steps.find((step) => step.recommended) || current;
  const completedCount = steps.filter((step) => step.complete).length;
  return (
    <section className="rounded-xl border bg-card p-4 shadow-sm">
      <div className="mb-4 flex flex-wrap items-start justify-between gap-3">
        <div>
          <div className="text-sm font-medium text-muted-foreground">Guided setup</div>
          <h3 className="mt-1 text-lg font-semibold">Step {current.index + 1} of {steps.length}: {current.label}</h3>
          <p className="mt-1 text-sm text-muted-foreground">{current.description}</p>
        </div>
        <div className="flex items-center gap-2">
          <Badge variant="secondary">{completedCount}/{steps.length} complete</Badge>
          {recommended.id !== active && (
            <Button size="sm" variant="outline" onClick={() => onChange(recommended.id)}>
              Go to next step
            </Button>
          )}
        </div>
      </div>
      <div role="tablist" aria-label="Setup steps" className="grid gap-3 md:grid-cols-4">
        {steps.map((step, index) => {
          const Icon = step.icon;
          const selected = active === step.id;
          const lineDone = index > 0 && steps[index - 1].complete;
          return (
            <button
              key={step.id}
              type="button"
              role="tab"
              aria-selected={selected}
              onClick={() => onChange(step.id)}
              className={cn(
                "group relative flex min-w-0 items-center gap-3 rounded-lg border p-3 text-left transition-colors",
                selected ? "border-primary/40 bg-primary/5" : "bg-background hover:bg-muted/40",
                step.blocked && "border-destructive/30 bg-destructive/5"
              )}
            >
              {index > 0 && (
                <span
                  className={cn(
                    "absolute -left-3 top-1/2 hidden h-px w-3 md:block",
                    lineDone ? "bg-primary" : "bg-border"
                  )}
                  aria-hidden="true"
                />
              )}
              <span
                className={cn(
                  "flex h-9 w-9 shrink-0 items-center justify-center rounded-full border bg-background",
                  step.complete && "border-primary bg-primary text-primary-foreground",
                  selected && !step.complete && !step.blocked && "border-primary text-primary",
                  step.blocked && "border-destructive text-destructive"
                )}
              >
                {step.complete ? <CheckCircle2 className="h-5 w-5" /> : <Icon className="h-4 w-4" />}
              </span>
              <span className="min-w-0">
                <span className="block truncate text-sm font-medium">{step.shortLabel}</span>
                <span className="block truncate text-xs text-muted-foreground">
                  {step.blocked ? "Action needed" : step.complete ? "Complete" : step.recommended ? "Next" : "Upcoming"}
                </span>
              </span>
            </button>
          );
        })}
      </div>
      <div className="mt-4 rounded-lg border bg-muted/30 px-3 py-2 text-sm text-muted-foreground">
        <span className="font-medium text-foreground">Your next action:</span> {current.action}
      </div>
    </section>
  );
}

function KeyValue({ label, value }: { label: string; value?: string | number | null }) {
  if (value == null || value === "") return null;
  return (
    <div className="min-w-0">
      <div className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">{label}</div>
      <div className="truncate text-sm">{value}</div>
    </div>
  );
}

interface ActionState {
  running: boolean;
  activeAction?: string | null;
}

function ActionButton({
  action,
  actionState,
  disabled,
  children,
  ...props
}: ComponentProps<typeof Button> & { action: string; actionState: ActionState; children: ReactNode }) {
  const isRunning = actionState.running && actionState.activeAction === action;
  return (
    <Button {...props} disabled={disabled || actionState.running}>
      {isRunning ? <Loader2 className="h-4 w-4 animate-spin" /> : null}
      {children}
    </Button>
  );
}

function MetricChip({ label, value }: { label: string; value?: string | number | null }) {
  if (value == null || value === "") return null;
  return (
    <Badge variant="secondary" className="gap-1">
      <span className="text-muted-foreground">{label}</span>
      <span>{typeof value === "number" ? value.toLocaleString() : value}</span>
    </Badge>
  );
}

function money(value?: number | null): string {
  if (typeof value !== "number" || Number.isNaN(value)) return "";
  return `$${value.toFixed(2)}`;
}

function duckdbTableLabel(name: string): string {
  const labels: Record<string, string> = {
    local_person_profiles: "Person profiles",
    local_people_positions: "Role / position vectors",
    local_summaries: "Person summary vectors",
    local_people_education: "Person education rows",
    local_education: "School lookup rows",
    local_companies: "Company vectors",
  };
  return labels[name] || name;
}

function formatBytes(value?: number | null): string {
  const bytes = Number(value || 0);
  if (!bytes) return "";
  const units = ["B", "KB", "MB", "GB"];
  let size = bytes;
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024;
    unit += 1;
  }
  return `${size.toFixed(size >= 10 || unit === 0 ? 0 : 1)} ${units[unit]}`;
}

function paidCallTotal(value?: Record<string, number>): number {
  if (!value) return 0;
  return Object.values(value).reduce((total, count) => total + (Number(count) || 0), 0);
}

function indexLabel(status?: string): string {
  const normalized = String(status || "unknown").toLowerCase();
  if (normalized === "ready" || normalized === "search_ready") return "Ready";
  if (normalized === "needs_processing" || normalized === "people_csv_ready_for_processing") return "Update available";
  if (normalized === "records_only_duckdb_missing") return "Build local DuckDB";
  if (normalized === "not_ready") return "Not ready";
  return normalized.replace(/_/g, " ");
}

function uniqueLabels(values: string[]): string[] {
  return Array.from(new Set(values.map((value) => value.trim()).filter(Boolean)));
}

function SourcePills({ values, maxVisible = 4 }: { values: string[]; maxVisible?: number }) {
  const labels = uniqueLabels(values);
  if (!labels.length) return <span className="text-sm text-muted-foreground">None</span>;
  const visible = labels.slice(0, maxVisible);
  const hiddenCount = Math.max(0, labels.length - visible.length);
  return (
    <div className="flex max-h-24 flex-wrap gap-1.5 overflow-hidden">
      {visible.map((value) => (
        <Badge key={value} variant="secondary" className="max-w-full truncate" title={value}>
          {value}
        </Badge>
      ))}
      {hiddenCount > 0 && (
        <Badge variant="outline" className="shrink-0">
          +{hiddenCount} more
        </Badge>
      )}
    </div>
  );
}

function linkedGmailAccounts(source?: SetupSourceStatus): string[] {
  if (!source) return [];
  const selected = stringList(source.config.selected_accounts);
  const accountEmails = stringList(source.config.account_emails);
  return uniqueLabels(selected.length ? selected : accountEmails.length ? accountEmails : source.usernames);
}

interface LinkTableRow {
  id: LinkRowId;
  sourceId: SetupSourceId;
  label: string;
  done: boolean;
  skipped: boolean;
  status: string;
  chips: string[];
}

function buildLinkRows(status: SetupStatusResponse): LinkTableRow[] {
  const byId = Object.fromEntries(status.accounts.sources.map((source) => [source.id, source]));
  const gmail = byId.gmail;
  const linkedin = byId.linkedin_csv;
  const messages = byId.messages;
  const twitter = byId.twitter;
  const messagesConfig = sourceConfig(messages);
  const iMessage = (messagesConfig.imessage || {}) as Record<string, unknown>;
  const whatsApp = (messagesConfig.whatsapp || {}) as Record<string, unknown>;
  const iMessageReady = boolValue(iMessage.readable) || stringValue(iMessage.status) === "ready";
  const whatsAppReady = boolValue(whatsApp.authenticated) || ["authenticated", "linked"].includes(stringValue(whatsApp.status));

  return [
    {
      id: "gmail",
      sourceId: "gmail",
      label: "Gmail",
      done: Boolean(gmail?.linked),
      skipped: Boolean(gmail?.skipped),
      status: gmail?.status || "available",
      chips: linkedGmailAccounts(gmail),
    },
    {
      id: "linkedin_csv",
      sourceId: "linkedin_csv",
      label: "LinkedIn CSV",
      done: Boolean(linkedin?.linked),
      skipped: Boolean(linkedin?.skipped),
      status: linkedin?.status || "available",
      chips: [],
    },
    {
      id: "imessage",
      sourceId: "messages",
      label: "iMessage",
      done: iMessageReady,
      skipped: Boolean(messages?.skipped),
      status: iMessageReady ? "linked" : stringValue(iMessage.status) || "available",
      chips: iMessageReady ? ["Access to DB"] : [],
    },
    {
      id: "whatsapp",
      sourceId: "messages",
      label: "WhatsApp",
      done: whatsAppReady,
      skipped: Boolean(messages?.skipped),
      status: whatsAppReady ? "linked" : stringValue(whatsApp.status) || "available",
      chips: whatsAppReady ? ["Authenticated"] : [],
    },
    {
      id: "twitter",
      sourceId: "twitter",
      label: "Twitter/X",
      done: Boolean(twitter?.linked),
      skipped: Boolean(twitter?.skipped),
      status: twitter?.status || "available",
      chips: [],
    },
  ];
}

function SourcePanel({
  source,
  onRun,
  embedded = false,
}: {
  source: SetupSourceStatus;
  onRun: (body: Record<string, unknown>) => void;
  embedded?: boolean;
}) {
  const [email, setEmail] = useState("");
  const [csvPath, setCsvPath] = useState("");
  const [sourceLabel, setSourceLabel] = useState("");
  const [handle, setHandle] = useState("");
  const [isLinkedInUploading, setIsLinkedInUploading] = useState(false);
  const [linkedinUploadError, setLinkedinUploadError] = useState("");
  const [selectedLinkedInFile, setSelectedLinkedInFile] = useState("");
  const selected = stringList(source.config.selected_accounts);
  const pending = stringList(source.config.pending_accounts);
  const accountEmails = stringList(source.config.account_emails);
  const gmailAccounts = selected.length ? selected : accountEmails;
  const linkedInCsvPath = stringValue(source.config.csv_path) || source.artifacts[0] || "";
  const linkedInSourceLabel = stringValue(source.config.source_label) || source.usernames[0] || "";
  const twitterHandle = stringValue(source.config.handle) || source.usernames[0] || "";

  useEffect(() => {
    if (source.id !== "linkedin_csv") return;
    if (!csvPath && linkedInCsvPath) {
      setCsvPath(linkedInCsvPath);
    }
    if (!sourceLabel && linkedInSourceLabel) {
      setSourceLabel(linkedInSourceLabel);
    }
  }, [csvPath, linkedInCsvPath, linkedInSourceLabel, source.id, sourceLabel]);

  const chooseLinkedInCsvFile = async (file?: File) => {
    if (!file) return;
    setSelectedLinkedInFile(file.name);
    setLinkedinUploadError("");
    setIsLinkedInUploading(true);
    try {
      const uploaded = await uploadLinkedInCsv(file);
      setCsvPath(uploaded.path);
      if (!sourceLabel.trim()) {
        setSourceLabel(linkedInSourceLabel || "linkedin");
      }
    } catch (err) {
      setLinkedinUploadError(err instanceof Error ? err.message : "Failed to copy LinkedIn CSV");
    } finally {
      setIsLinkedInUploading(false);
    }
  };

  return (
    <section className={cn(embedded ? "rounded-md border bg-background p-4" : "rounded-md border bg-card p-4")}>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="flex min-w-0 items-start gap-3">
          <SourceIcon source={source} />
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <h3 className="text-base font-semibold">{source.label}</h3>
              <StatusBadge status={source.status} />
              <SourceStateIcon source={source} />
            </div>
            {source.notes && <p className="mt-1 line-clamp-2 text-sm text-muted-foreground">{source.notes}</p>}
          </div>
        </div>
        {!source.linked && !source.skipped && (
          <Button variant="outline" size="sm" onClick={() => onRun({ action: "skip-source", source: source.id })}>
            Skip
          </Button>
        )}
      </div>

      <div className="mt-4 grid gap-3 lg:grid-cols-3">
        <KeyValue label="Last checked" value={updatedLabel(source.lastCheckedAt)} />
        <KeyValue label="Last success" value={updatedLabel(source.lastSuccessAt)} />
        <KeyValue label="Linked items" value={source.usernames.length || source.artifacts.length || (source.linked ? 1 : 0)} />
      </div>

      {source.id === "gmail" && (
        <div className="mt-4 space-y-3">
          <div className="grid gap-3 md:grid-cols-2">
            <div>
              <div className="mb-1 text-xs font-medium text-muted-foreground">Linked Gmail accounts</div>
              <SourcePills values={gmailAccounts} />
            </div>
            {pending.length > 0 && (
              <div>
                <div className="mb-1 text-xs font-medium text-muted-foreground">Pending Gmail accounts</div>
                <SourcePills values={pending} />
                <Button className="mt-2" size="sm" variant="outline" onClick={() => onRun({ action: "gmail-link-emails", emails: pending })}>
                  <Link2 className="h-4 w-4" /> Complete linking
                </Button>
              </div>
            )}
          </div>
          <div className="flex flex-wrap items-start gap-2">
            <textarea
              value={email}
              onChange={(event) => setEmail(event.target.value)}
              placeholder={"name@example.com\nother@example.com"}
              rows={3}
              className="min-h-20 w-full min-w-72 flex-1 rounded-md border border-input bg-background px-3 py-2 text-sm ring-offset-background placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50"
            />
            <Button size="sm" onClick={() => onRun({ action: "gmail-link-emails", emails: email })} disabled={!email.trim()}>
              <Link2 className="h-4 w-4" /> Link accounts
            </Button>
          </div>
        </div>
      )}

      {source.id === "linkedin_csv" && (
        <div className="mt-4 space-y-3">
          <div className="grid gap-3 md:grid-cols-[1fr_220px]">
            <KeyValue label="Linked file" value={linkedInCsvPath} />
            <KeyValue label="Import name" value={linkedInSourceLabel} />
          </div>
          <div className="grid gap-2 md:grid-cols-[minmax(0,1fr)_180px_auto] md:items-end">
            <div className="min-w-0">
              <div className="mb-1 text-xs font-medium text-muted-foreground">LinkedIn export</div>
              <div className="flex flex-wrap gap-2">
                <label className={cn(
                  "inline-flex h-9 cursor-pointer items-center justify-center gap-2 rounded-md border border-input bg-background px-3 text-sm font-medium shadow-sm hover:bg-accent hover:text-accent-foreground",
                  isLinkedInUploading && "pointer-events-none opacity-60",
                )}>
                  {isLinkedInUploading ? <Loader2 className="h-4 w-4 animate-spin" /> : <FileText className="h-4 w-4" />}
                  {isLinkedInUploading ? "Copying..." : "Choose CSV"}
                  <input
                    type="file"
                    accept=".csv,text/csv"
                    className="sr-only"
                    disabled={isLinkedInUploading}
                    onChange={(event) => chooseLinkedInCsvFile(event.currentTarget.files?.[0])}
                  />
                </label>
                <Input
                  value={csvPath}
                  readOnly
                  placeholder="No CSV selected"
                  className="min-w-72 flex-1"
                />
              </div>
              {selectedLinkedInFile && <div className="mt-1 text-xs text-muted-foreground">Selected {selectedLinkedInFile}</div>}
              {linkedinUploadError && <div className="mt-1 text-xs text-destructive">{linkedinUploadError}</div>}
            </div>
            <div>
              <div className="mb-1 text-xs font-medium text-muted-foreground">Import name</div>
              <Input
                value={sourceLabel}
                onChange={(event) => setSourceLabel(event.target.value)}
                placeholder={linkedInSourceLabel || "name"}
                className="w-full"
              />
            </div>
            <Button size="sm" onClick={() => onRun({ action: "linkedin-csv", csvPath, sourceLabel })} disabled={!csvPath.trim() || !sourceLabel.trim()}>
              Link
            </Button>
          </div>
        </div>
      )}

      {source.id === "twitter" && (
        <div className="mt-4 space-y-3">
          <KeyValue label="Linked handle" value={twitterHandle ? `@${twitterHandle}` : ""} />
          <div className="flex flex-wrap gap-2">
            <Input
              value={handle}
              onChange={(event) => setHandle(event.target.value)}
              placeholder={twitterHandle ? `@${twitterHandle}` : "@handle"}
              className="w-full sm:w-72"
            />
            <Button size="sm" onClick={() => onRun({ action: "twitter-handle", handle })} disabled={!handle.trim()}>
              Link
            </Button>
          </div>
        </div>
      )}
    </section>
  );
}

function MessagesChannelPanel({
  source,
  channel,
  onRun,
  jobs = [],
}: {
  source: SetupSourceStatus;
  channel: "imessage" | "whatsapp";
  onRun: (body: Record<string, unknown>) => void;
  jobs?: SetupJob[];
}) {
  const iMessage = (source.config.imessage || {}) as Record<string, unknown>;
  const whatsApp = (source.config.whatsapp || {}) as Record<string, unknown>;
  const iMessageReadable = boolValue(iMessage.readable) || stringValue(iMessage.status) === "ready";
  const whatsAppAuthenticated = boolValue(whatsApp.authenticated)
    || ["authenticated", "linked"].includes(stringValue(whatsApp.status));
  const whatsAppQrPath = stringValue(whatsApp.qr_png);
  const whatsAppQrPage = stringValue(whatsApp.qr_page);
  const whatsAppQrUpdatedAt = stringValue(whatsApp.qr_updated_at);
  const whatsAppQrSrc = whatsAppQrPath
    ? `/local-api/setup/whatsapp-qr?path=${encodeURIComponent(whatsAppQrPath)}${whatsAppQrUpdatedAt ? `&t=${encodeURIComponent(whatsAppQrUpdatedAt)}` : ""}`
    : "";
  const isAuthenticating = jobs.some((job) => job.action === "whatsapp-auth" && job.status === "running");

  if (channel === "imessage") {
    return (
      <section className="rounded-md border bg-background p-4">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <h3 className="text-base font-semibold">iMessage</h3>
              {iMessageReadable ? (
                <Badge variant="default" className="gap-1">
                  <CheckCircle2 className="h-3.5 w-3.5" /> Access to DB
                </Badge>
              ) : (
                <StatusBadge status="permission_required" />
              )}
            </div>
            <KeyValue label="chat.db" value={stringValue(iMessage.chat_db)} />
          </div>
          <div className="flex flex-wrap gap-2">
            {iMessageReadable ? (
              <Button size="sm" variant="outline" onClick={() => onRun({ action: "messages-link", skipWhatsapp: true })}>
                Link
              </Button>
            ) : (
              <Button size="sm" className="bg-amber-600 text-white hover:bg-amber-700" onClick={() => onRun({ action: "open-message-permissions" })}>
                Grant Access to DB
              </Button>
            )}
          </div>
        </div>
      </section>
    );
  }

  return (
    <section className="rounded-md border bg-background p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="text-base font-semibold">WhatsApp</h3>
            {whatsAppAuthenticated ? (
              <Badge variant="default" className="gap-1">
                <CheckCircle2 className="h-3.5 w-3.5" /> Authenticated
              </Badge>
            ) : (
              <StatusBadge status="not_authenticated" />
            )}
          </div>
          <KeyValue label="Account" value={whatsAppAuthenticated ? "authenticated" : "not authenticated"} />
          {!whatsAppAuthenticated && whatsAppQrPage && (
            <KeyValue label="QR file" value={whatsAppQrPage} />
          )}
        </div>
        <div className="flex flex-wrap gap-2">
          <Button
            size="sm"
            variant={whatsAppAuthenticated ? "outline" : "default"}
            onClick={() => onRun({ action: "whatsapp-auth" })}
            disabled={whatsAppAuthenticated || isAuthenticating}
          >
            {isAuthenticating && !whatsAppQrSrc ? (
              <><Loader2 className="h-4 w-4 animate-spin" /> Generating QR…</>
            ) : whatsAppAuthenticated ? "Authenticated" : whatsAppQrSrc ? "Refresh QR" : "Authenticate WhatsApp"}
          </Button>
        </div>
      </div>
      {!whatsAppAuthenticated && isAuthenticating && !whatsAppQrSrc && (
        <div className="mt-4 flex items-center gap-3 rounded-md border bg-muted/20 p-4 text-sm text-muted-foreground">
          <Loader2 className="h-5 w-5 animate-spin shrink-0" />
          <span>Starting WhatsApp link session — QR code will appear here shortly…</span>
        </div>
      )}
      {!whatsAppAuthenticated && whatsAppQrSrc && (
        <div className="mt-4 rounded-md border bg-muted/20 p-4">
          <div className="mb-3">
            <div className="text-sm font-medium">Scan in WhatsApp</div>
            <div className="text-xs text-muted-foreground">WhatsApp &gt; Settings &gt; Linked Devices</div>
          </div>
          <img
            src={whatsAppQrSrc}
            alt="WhatsApp QR code"
            className="mx-auto block w-full max-w-[360px] rounded-md border bg-white p-4"
          />
        </div>
      )}
    </section>
  );
}

function AccountLinkingTab({
  status,
  onRun,
}: {
  status: SetupStatusResponse;
  onRun: (body: Record<string, unknown>) => void;
}) {
  const rows = buildLinkRows(status);
  const [expandedRows, setExpandedRows] = useState<LinkRowId[]>(() => {
    const firstAvailable = rows.find((row) => !row.done && !row.skipped);
    return firstAvailable ? [firstAvailable.id] : [];
  });
  const expandedSet = new Set(expandedRows);
  const toggleExpanded = (rowId: LinkRowId) => {
    setExpandedRows((current) => (
      current.includes(rowId)
        ? current.filter((candidate) => candidate !== rowId)
        : [...current, rowId]
    ));
  };

  return (
    <div className="space-y-4">
      <section className="rounded-md border bg-card">
        <div className="flex flex-wrap items-center justify-between gap-2 border-b p-4">
          <h3 className="text-base font-semibold">Accounts and sources</h3>
          <Button
            size="sm"
            variant="outline"
            onClick={() => setExpandedRows(expandedRows.length === rows.length ? [] : rows.map((row) => row.id))}
          >
            {expandedRows.length === rows.length ? "Collapse all" : "Expand all"}
          </Button>
        </div>
        <div className="divide-y">
          {rows.map((row) => {
            const expanded = expandedSet.has(row.id);
            const source = status.accounts.sources.find((candidate) => candidate.id === row.sourceId);
            return (
              <div key={row.id}>
                <div className="grid gap-3 px-4 py-3 md:grid-cols-[minmax(0,1fr)_minmax(0,1.3fr)_120px_auto] md:items-center">
                  <button
                    type="button"
                    onClick={() => toggleExpanded(row.id)}
                    className="flex min-w-0 items-center gap-2 text-left"
                    aria-expanded={expanded}
                  >
                    {expanded ? <ChevronDown className="h-4 w-4 shrink-0 text-muted-foreground" /> : <ChevronRight className="h-4 w-4 shrink-0 text-muted-foreground" />}
                    <span className="truncate text-sm font-medium">{row.label}</span>
                  </button>
                  <div className="min-w-0">
                    {row.chips.length > 0 ? <SourcePills values={row.chips} /> : <span className="text-sm text-muted-foreground">Available</span>}
                  </div>
                  <div>
                    {row.done ? (
                      <Badge variant="default">Linked</Badge>
                    ) : row.skipped ? (
                      <Badge variant="secondary">Skipped</Badge>
                    ) : (
                      <Badge variant="outline">{row.status === "permission_required" ? "Permission needed" : "Available"}</Badge>
                    )}
                  </div>
                  <div className="flex justify-end gap-2">
                    {!row.done && !row.skipped && (
                      <Button size="sm" variant="outline" onClick={() => source && onRun({ action: "skip-source", source: source.id })}>
                        Skip
                      </Button>
                    )}
                    <Button size="sm" variant="outline" onClick={() => toggleExpanded(row.id)}>
                      {expanded ? "Collapse" : row.done ? "Details" : "Link"}
                    </Button>
                  </div>
                </div>
                {expanded && source && (
                  <div className="border-t bg-muted/20 p-4">
                    {row.id === "imessage" || row.id === "whatsapp" ? (
                      <MessagesChannelPanel source={source} channel={row.id} onRun={onRun} jobs={status.jobs} />
                    ) : (
                      <SourcePanel source={source} onRun={onRun} embedded />
                    )}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      </section>
    </div>
  );
}

function ImportSourceRow({
  source,
  expanded,
  onToggle,
  onRun,
  actionState,
}: {
  source: SetupImportSource;
  expanded: boolean;
  onToggle: () => void;
  onRun: (body: Record<string, unknown>) => void;
  actionState: ActionState;
}) {
  const Icon = SOURCE_ICONS[source.sourceId as SetupSourceId] || Database;
  const canRun = source.linked && !source.skipped && source.runnable !== false;
  const updated = source.linked && !source.skipped ? updatedLabel(source.updatedAt) : "";
  const accountLabel = source.accountEmail || (source.accountCount ? `${source.accountCount.toLocaleString()} accounts` : "");
  return (
    <div className="border-b last:border-b-0">
      <button
        type="button"
        className="grid w-full gap-3 px-4 py-3 text-left transition-colors hover:bg-muted/30 md:grid-cols-[minmax(0,1fr)_140px_auto] md:items-center"
        onClick={onToggle}
        aria-expanded={expanded}
      >
        <div className="flex min-w-0 items-start gap-3">
          <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md border bg-background">
            <Icon className="h-4 w-4 text-muted-foreground" />
          </div>
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <div className="truncate text-sm font-medium">{source.label}</div>
              <StatusBadge status={source.status} />
            </div>
            <div className="mt-1 truncate text-xs text-muted-foreground">
              {source.disabledReason || (updated ? `Last refreshed ${updated}` : source.skipped ? "" : "No refresh yet")}
            </div>
          </div>
        </div>
        <KeyValue label="Updated" value={updated} />
        <div className="flex items-center justify-end gap-2 text-xs font-medium text-muted-foreground">
          {expanded ? <ChevronDown className="h-4 w-4" /> : <ChevronRight className="h-4 w-4" />}
          <span>{expanded ? "Hide stats" : "Stats"}</span>
        </div>
      </button>
      {expanded && (
        <div className="border-t bg-muted/20 px-4 py-4">
          <div className="grid gap-4 md:grid-cols-[minmax(0,1fr)_auto] md:items-end">
            <div className="space-y-3">
              <div className="flex flex-wrap gap-2">
                <MetricChip label="Status" value={statusDisplayLabel(source.status)} />
                <MetricChip label="Accounts" value={accountLabel} />
                <MetricChip label="Runnable" value={canRun ? "Yes" : "No"} />
              </div>
              <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
                <KeyValue label="Source" value={source.label} />
                <KeyValue label="Last refreshed" value={updated} />
                <KeyValue label="Run ID" value={source.runId} />
                <KeyValue label="Blocked reason" value={source.disabledReason} />
              </div>
            </div>
            <div className="flex justify-end">
              <ActionButton
                action="import-source"
                actionState={actionState}
                size="sm"
                onClick={() => onRun({ action: "import-source", source: source.id })}
                disabled={!canRun}
              >
                <Play className="h-4 w-4" /> Import {source.label}
              </ActionButton>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function ImportTab({
  status,
  onRun,
  actionState,
}: {
  status: SetupStatusResponse;
  onRun: (body: Record<string, unknown>) => void;
  actionState: ActionState;
}) {
  const importSources = status.import.sources || [];
  const [expandedSources, setExpandedSources] = useState<Set<string>>(() => new Set());
  const toggleExpanded = (id: string) => {
    setExpandedSources((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };
  return (
    <div className="space-y-4">
      <section className="rounded-md border bg-card">
        <div className="flex flex-wrap items-center justify-between gap-3 border-b p-4">
          <div>
            <h3 className="text-base font-semibold">Linked account imports</h3>
          </div>
          <div className="flex flex-wrap gap-2">
            <ActionButton action="import" actionState={actionState} onClick={() => onRun({ action: "import" })}>
              <Play className="h-4 w-4" /> Run Full Import
            </ActionButton>
          </div>
        </div>
        <div>
          {importSources.length ? (
            importSources.map((source) => (
              <ImportSourceRow
                key={source.id}
                source={source}
                expanded={expandedSources.has(source.id)}
                onToggle={() => toggleExpanded(source.id)}
                onRun={onRun}
                actionState={actionState}
              />
            ))
          ) : (
            <div className="p-6 text-sm text-muted-foreground">No linked accounts found.</div>
          )}
        </div>
      </section>
    </div>
  );
}

function EnrichmentSourceRow({
  source,
  importSource,
  messagesReviewReady = false,
  messagesParallelBlocked = false,
  messagesReviewBlocked = false,
  onRun,
  actionState,
}: {
  source: SetupEnrichmentSource;
  importSource?: SetupImportSource;
  messagesReviewReady?: boolean;
  messagesParallelBlocked?: boolean;
  messagesReviewBlocked?: boolean;
  onRun: (body: Record<string, unknown>) => void;
  actionState: ActionState;
}) {
  const Icon = SOURCE_ICONS[source.id as SetupSourceId] || Sparkles;
  const canRun = Boolean(importSource?.linked && !importSource.skipped && importSource.runnable !== false);
  const sourceSkipped = String(source.status || "").toLowerCase() === "skipped";
  const updated = sourceSkipped ? "" : updatedLabel(source.updatedAt || importSource?.updatedAt);
  const skippedLabel = source.id === "messages" ? "Skipped" : "Not found";
  const hasUnresolved = (source.unresolved || 0) > 0;
  const canReviewMessages = source.id === "messages" && canRun && messagesReviewReady;
  const costLabel = source.estimatedCostUsd != null && source.estimatedCostUsd > 0
    ? `~$${source.estimatedCostUsd.toFixed(2)}` : null;
  const completingMessages = source.id === "messages" && actionState.running && actionState.activeAction === "messages-complete-review";
  return (
    <div className="border-b px-4 py-3 last:border-b-0">
      <div className="grid gap-3 lg:grid-cols-[minmax(0,1.4fr)_120px_120px_120px_auto] lg:items-center">
        <div className="flex min-w-0 items-start gap-3">
          <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md border bg-background">
            <Icon className="h-4 w-4 text-muted-foreground" />
          </div>
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <div className="truncate text-sm font-medium">{source.label}</div>
              <StatusBadge status={source.status} />
            </div>
            <div className="mt-1 truncate text-xs text-muted-foreground">
              {updated ? `Last refreshed ${updated}` : sourceSkipped ? "" : "No refresh yet"}
            </div>
          </div>
        </div>
        <KeyValue label="Candidates" value={source.candidates.toLocaleString()} />
        <KeyValue label="Profiles found" value={source.enriched.toLocaleString()} />
        <KeyValue label={skippedLabel} value={source.skipped.toLocaleString()} />
        <div className="flex justify-end gap-2">
          {canReviewMessages && (
            <Button size="sm" variant="outline" onClick={() => { window.location.href = "/setup/imessage/review"; }}>
              <MessageSquare className="h-4 w-4" /> Review
            </Button>
          )}
          {messagesParallelBlocked && (
            <ActionButton
              action="messages-approve-continue"
              actionState={actionState}
              size="sm"
              onClick={() => onRun({ action: "messages-approve-continue" })}
            >
              <Play className="h-4 w-4" /> Approve
            </ActionButton>
          )}
          <ActionButton
            action="enrich-source"
            actionState={actionState}
            size="sm"
            onClick={() => importSource && onRun({ action: "enrich-source", source: importSource.id })}
            disabled={!canRun}
          >
            <Sparkles className="h-4 w-4" /> Enrich
          </ActionButton>
        </div>
      </div>
      {messagesReviewBlocked && canRun && (
        <div className="mt-3 flex flex-wrap items-center gap-3 rounded-md border border-amber-300 bg-amber-50 px-4 py-3 text-amber-950">
          <div className="flex-1 text-sm">
            <span className="font-medium">Review required.</span>{" "}
            <span>Please review and approve people to add into your local network, then click Complete.</span>
          </div>
          <Button size="sm" onClick={() => { window.location.href = "/setup/imessage/review"; }}>
            <MessageSquare className="h-4 w-4" /> Review
          </Button>
        </div>
      )}
      {completingMessages && (
        <div className="mt-3 flex items-center gap-2 rounded-md border bg-muted/30 px-4 py-3 text-sm text-muted-foreground">
          <Loader2 className="h-4 w-4 animate-spin" /> Materializing Messages people and profile enrichment…
        </div>
      )}
      {hasUnresolved && canRun && (
        <div className="mt-3 flex flex-wrap items-center gap-3 rounded-md border bg-muted/30 px-4 py-3">
          <div className="flex-1 text-sm">
            <span className="font-medium">{source.unresolved.toLocaleString()}</span>{" "}
            <span className="text-muted-foreground">contacts need email→LinkedIn resolution via Parallel</span>
            {costLabel && <span className="ml-2 text-muted-foreground">({costLabel} est.)</span>}
          </div>
          <ActionButton
            action="enrich-source"
            actionState={actionState}
            size="sm"
            onClick={() => importSource && onRun({ action: "enrich-source", source: importSource.id, approveSpend: true })}
          >
            <Play className="h-4 w-4" /> Approve
          </ActionButton>
        </div>
      )}
    </div>
  );
}

function EnrichmentTab({
  status,
  onRun,
  actionState,
}: {
  status: SetupStatusResponse;
  onRun: (body: Record<string, unknown>) => void;
  actionState: ActionState;
}) {
  const sources = status.enrichment.sources || [];
  const importSourceFor = (source: SetupEnrichmentSource) => {
    if (source.id === "gmail") {
      return status.import.sources.find((candidate) => candidate.sourceId === "gmail" && candidate.linked && !candidate.skipped)
        || status.import.sources.find((candidate) => candidate.sourceId === "gmail");
    }
    return status.import.sources.find((candidate) => candidate.id === source.id || candidate.sourceId === source.id);
  };
  const messagesReviewReady = Boolean(status.review.exists && (status.review.counts?.total || 0) > 0);
  const messagesParallelBlocked = String(status.messages.currentBlock?.status || "") === "blocked_approval"
    && String(status.messages.currentBlock?.approval_type || "") === "parallel";
  const messagesReviewBlocked = String(status.messages.currentBlock?.status || "") === "blocked_user_action";

  return (
    <div className="space-y-4">
      <section className="rounded-md border bg-card">
        <div className="flex flex-wrap items-center justify-between gap-3 border-b p-4">
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="text-base font-semibold">Enrichment</h3>
            <StatusBadge status={status.enrichment.status} />
            <MetricChip label="Candidates" value={status.enrichment.totalCandidates} />
            <MetricChip label="Profiles found" value={status.enrichment.totalEnriched} />
          </div>
          <div className="flex flex-wrap gap-2">
            <ActionButton action="enrich-all" actionState={actionState} onClick={() => onRun({ action: "enrich-all" })}>
              <Play className="h-4 w-4" /> Run All
            </ActionButton>
          </div>
        </div>
        <div>
          {sources.length ? (
            sources.map((source) => (
              <EnrichmentSourceRow
                key={source.id}
                source={source}
                importSource={importSourceFor(source)}
                messagesReviewReady={source.id === "messages" && messagesReviewReady}
                messagesParallelBlocked={source.id === "messages" && messagesParallelBlocked}
                messagesReviewBlocked={source.id === "messages" && messagesReviewBlocked}
                onRun={onRun}
                actionState={actionState}
              />
            ))
          ) : (
            <div className="p-6 text-sm text-muted-foreground">No enrichment sources found.</div>
          )}
        </div>
      </section>
    </div>
  );
}

function IndexTab({ status, onRun, actionState }: { status: SetupStatusResponse; onRun: (body: Record<string, unknown>) => void; actionState: ActionState }) {
  const readiness = status.index.readiness || status.setup.phases.index;
  const estimate = status.index.processingEstimate || {};
  const paidCalls = paidCallTotal(estimate.estimatedPaidCalls);
  const cost = money(estimate.totalEstimatedUsd);
  const counts = estimate.counts || {};
  const bootstrapRecordCount = Number(status.index.bootstrapRecords?.nonemptyRecordFiles || 0);
  const localRecordsMode = String(estimate.status || "") === "local_records_restore" || (bootstrapRecordCount > 0 && !status.index.duckdbTables?.length);
  const duckdbRepaired = status.index.duckdbRepair?.status === "ok";
  const hasProviderEstimate = paidCalls > 0 || (estimate.totalEstimatedUsd || 0) > 0;
  const requiresProviderSpend = !localRecordsMode && hasProviderEstimate;
  const updateAvailable = ["needs_processing", "people_csv_ready_for_processing"].includes(String(readiness || "").toLowerCase())
    || status.index.reason === "search_index_stale_for_people_csv"
    || hasProviderEstimate;
  const showProviderEstimate = hasProviderEstimate && !localRecordsMode;

  return (
    <div className="space-y-4">
      <section className="rounded-md border bg-card p-4">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0 space-y-4">
            <div className="flex flex-wrap items-center gap-2">
              <h3 className="text-base font-semibold">Local search index</h3>
              <Badge variant={updateAvailable ? "secondary" : phaseTone(readiness)}>{indexLabel(readiness)}</Badge>
              <MetricChip label="Total people" value={status.index.peopleRecords || 0} />
              <MetricChip label="Bootstrap record files" value={bootstrapRecordCount || null} />
              {showProviderEstimate && <MetricChip label="Cost" value={cost || "$0.00"} />}
              {showProviderEstimate && <MetricChip label="Paid calls" value={paidCalls} />}
              <MetricChip label="DuckDB" value={formatBytes(status.index.duckdbSizeBytes)} />
            </div>
            {localRecordsMode ? (
              <div className="text-sm text-muted-foreground">
                Bootstrap search records are available locally. Processing will build the DuckDB tables from those records without provider calls.
              </div>
            ) : duckdbRepaired ? (
              <div className="text-sm text-muted-foreground">
                Built local DuckDB tables from bootstrap records. A full rebuild from the current people.csv would use the estimate below.
              </div>
            ) : showProviderEstimate ? (
              <div className="text-sm text-muted-foreground">
                Full processing dry-run found provider work for the current people.csv. Review the estimate before rebuilding.
              </div>
            ) : updateAvailable ? (
              <div className="text-sm text-muted-foreground">
                Update available. The current index was built from an older people.csv.
              </div>
            ) : null}
            {estimate.error && (
              <div className="rounded-md border border-destructive/30 bg-destructive/5 px-3 py-2 text-sm text-destructive">
                {estimate.error}
              </div>
            )}
            <div className="grid gap-3 md:grid-cols-2">
              <KeyValue label="DuckDB" value={status.index.duckdb} />
              <KeyValue label="Last updated" value={updatedLabel(status.index.duckdbUpdatedAt)} />
              <KeyValue label="People CSV" value={status.index.peopleCsv} />
              <KeyValue label="People SHA" value={status.index.peopleSha256} />
              <KeyValue label="Input SHA" value={status.index.indexInputSha256} />
            </div>
            <div className="grid gap-4 lg:grid-cols-2">
              <div className="overflow-hidden rounded-md border">
                <div className="border-b bg-muted/40 px-3 py-2 text-sm font-medium">DuckDB tables</div>
                {status.index.duckdbTables?.length ? (
                  <table className="w-full text-sm">
                    <thead className="bg-muted/30 text-left text-xs uppercase tracking-wide text-muted-foreground">
                      <tr>
                        <th className="px-3 py-2 font-medium">Table</th>
                        <th className="px-3 py-2 text-right font-medium">Rows</th>
                      </tr>
                    </thead>
                    <tbody>
                      {status.index.duckdbTables.map((table) => (
                        <tr key={table.name} className="border-t">
                          <td className="px-3 py-2">
                            <div>{duckdbTableLabel(table.name)}</div>
                            <div className="text-xs text-muted-foreground">{table.name}</div>
                          </td>
                          <td className="px-3 py-2 text-right tabular-nums">{table.rows.toLocaleString()}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                ) : (
                  <div className="px-3 py-4 text-sm text-muted-foreground">No local DuckDB tables found.</div>
                )}
              </div>
              <div className="overflow-hidden rounded-md border">
                <div className="border-b bg-muted/40 px-3 py-2 text-sm font-medium">Full processing dry-run</div>
                {localRecordsMode ? (
                  <div className="grid gap-3 p-3 sm:grid-cols-2">
                    <KeyValue label="Action" value="Build DuckDB from bootstrap records" />
                    <KeyValue label="Provider calls" value="None" />
                    <KeyValue label="Record files" value={bootstrapRecordCount.toLocaleString()} />
                    <KeyValue label="Source" value="operator bootstrap" />
                  </div>
                ) : showProviderEstimate ? (
                  <div className="grid gap-3 p-3 sm:grid-cols-2">
                    <KeyValue label="People" value={Number(counts.people || 0).toLocaleString()} />
                    <KeyValue label="Summaries" value={Number(counts.summaries || 0).toLocaleString()} />
                    <KeyValue label="Unique roles" value={Number(counts.unique_roles || 0).toLocaleString()} />
                    <KeyValue label="Companies" value={Number(counts.companies || 0).toLocaleString()} />
                    <KeyValue label="Role chunks" value={Number(counts.role_chunks || 0).toLocaleString()} />
                    <KeyValue label="Company chunks" value={Number(counts.company_chunks || 0).toLocaleString()} />
                  </div>
                ) : (
                  <div className="px-3 py-4 text-sm text-muted-foreground">No provider processing work detected by dry-run.</div>
                )}
              </div>
            </div>
          </div>
          <ActionButton action="index" actionState={actionState} onClick={() => onRun({ action: "index", approveProviderSpend: requiresProviderSpend })}>
            <Play className="h-4 w-4" /> {localRecordsMode ? "Build DuckDB" : requiresProviderSpend ? "Approve & Update" : "Process"}
          </ActionButton>
        </div>
      </section>
    </div>
  );
}

function JobPanel({
  job,
  onRunCommand,
}: {
  job?: SetupJob | null;
  onRunCommand: (command: string) => void;
}) {
  if (!job) return null;
  const commands = extractCommands(job);
  return (
    <section className="rounded-md border bg-card">
      <div className="flex flex-wrap items-center justify-between gap-2 border-b px-4 py-3">
          <div className="flex min-w-0 items-center gap-2">
            <Terminal className="h-4 w-4 text-muted-foreground" />
            <div className="min-w-0">
              <div className="truncate text-sm font-medium">{job.action}</div>
              <div className="truncate text-xs text-muted-foreground">{jobSummary(job)}</div>
            </div>
          </div>
          <StatusBadge status={job.status} />
      </div>
      {commands.length > 0 && (
        <div className="space-y-2 border-b p-4">
          {commands.map((command) => (
            <div key={command.command} className="flex flex-wrap items-center justify-between gap-2 rounded-md border bg-background px-3 py-2">
              <div className="min-w-0">
                <div className="text-sm font-medium capitalize">{command.label.replace(/_/g, " ")}</div>
                {command.description && <div className="text-xs text-muted-foreground">{command.description}</div>}
              </div>
              <Button size="sm" onClick={() => onRunCommand(command.command)}>
                <Play className="h-4 w-4" /> Run
              </Button>
            </div>
          ))}
        </div>
      )}
      <details open={job.status === "running"}>
        <summary className="cursor-pointer px-4 py-3 text-xs font-medium text-muted-foreground">Command and logs</summary>
        <div className="space-y-3 border-t px-4 py-3 text-xs text-muted-foreground">
          <div>
            <div className="mb-1 font-medium">Command</div>
            <pre className="max-h-32 overflow-auto rounded-md bg-muted/40 p-3 font-mono whitespace-pre-wrap break-words">
              {cleanJobText(job.command.join(" "))}
            </pre>
          </div>
          <div>
            <div className="mb-1 font-medium">Logs</div>
            <pre className="max-h-[28rem] overflow-auto rounded-md bg-muted/40 p-3 font-mono whitespace-pre-wrap break-words">
              {cleanJobText(job.log || [job.stdout, job.stderr].filter(Boolean).join("\n")) || "No output yet."}
            </pre>
          </div>
        </div>
      </details>
    </section>
  );
}

export function LocalSetupPage(_props: { onOpenMessagesReview: () => void }) {
  const [activeTab, setActiveTab] = useState<SetupTabId>(() => setupTabFromLocation());
  const [status, setStatus] = useState<SetupStatusResponse | null>(null);
  const [activeJobId, setActiveJobId] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setError(null);
    try {
      const nextStatus = await fetchSetupStatus();
      setStatus(nextStatus);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load setup status");
    } finally {
      setIsLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  useEffect(() => {
    const handlePopState = () => setActiveTab(setupTabFromLocation());
    window.addEventListener("popstate", handlePopState);
    return () => window.removeEventListener("popstate", handlePopState);
  }, []);

  useEffect(() => {
    if (!status || hasSetupTabParam()) return;
    const recommended = setupStepProgress(status, activeTab).find((step) => step.recommended)?.id;
    if (recommended && recommended !== activeTab) setActiveTab(recommended);
  }, [activeTab, status]);

  const running = status?.jobs.some((job) => job.status === "running") || false;
  useEffect(() => {
    if (!running) return undefined;
    const timer = window.setInterval(refresh, 2000);
    return () => window.clearInterval(timer);
  }, [refresh, running]);

  const activeJob = useMemo(() => {
    if (!status?.jobs.length) return null;
    return status.jobs.find((job) => job.id === activeJobId) || status.jobs[0];
  }, [activeJobId, status?.jobs]);
  const actionState: ActionState = {
    running,
    activeAction: activeJob?.status === "running" ? activeJob.action : null,
  };

  const runAction = async (body: Record<string, unknown>) => {
    setError(null);
    try {
      const response = await runSetupAction(body);
      setActiveJobId(response.job.id);
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to start setup action");
    }
  };

  const runCommand = (command: string) => {
    runAction({ action: "run-command", command });
  };

  const changeTab = (tab: SetupTabId) => {
    setActiveTab(tab);
    setSetupTabParam(tab);
  };

  if (isLoading && !status) {
    return (
      <div className="flex min-h-[60vh] items-center justify-center gap-2 text-muted-foreground">
        <Loader2 className="h-5 w-5 animate-spin" /> Loading setup
      </div>
    );
  }

  if (!status) {
    return (
      <div className="rounded-md border bg-card p-6 text-sm text-muted-foreground">
        Setup status is unavailable.
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <div>
        <h2 className="text-2xl font-semibold">Setup</h2>
        <div className="mt-1 flex flex-wrap items-center gap-2 text-sm text-muted-foreground">
          {refreshLabel(status) && (
            <span>{refreshLabel(status)}</span>
          )}
        </div>
      </div>

      {error && (
        <div className="rounded-md border border-destructive/40 bg-destructive/5 px-3 py-2 text-sm text-destructive">
          {error}
        </div>
      )}

      <SetupStepper active={activeTab} status={status} onChange={changeTab} />

      {activeTab === "link" && <AccountLinkingTab status={status} onRun={runAction} />}

      {activeTab === "import" && <ImportTab status={status} onRun={runAction} actionState={actionState} />}

      {activeTab === "enrichment" && <EnrichmentTab status={status} onRun={runAction} actionState={actionState} />}

      {activeTab === "index" && <IndexTab status={status} onRun={runAction} actionState={actionState} />}

      <JobPanel job={activeJob} onRunCommand={runCommand} />
    </div>
  );
}
