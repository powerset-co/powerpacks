import { useCallback, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { formatDistanceToNow } from "date-fns";
import {
  CheckCircle2,
  CircleAlert,
  CircleDot,
  Clock3,
  Database,
  HardDrive,
  Link2,
  Loader2,
  MessageSquare,
  Play,
  RefreshCcw,
  Sparkles,
  UploadCloud,
} from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { fetchSetupStatus, runSetupAction } from "./powerpacksApi";
import type { SetupJob, SetupSourceStatus, SetupStatusResponse } from "./types";

type SetupTabId = "link" | "import" | "enrichment" | "index";

interface LocalOnboardingPageProps {
  onOpenSetupTab: (tab: SetupTabId) => void;
  onOpenMessagesReview: () => void;
}

function updatedLabel(value?: string | null): string {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return formatDistanceToNow(date, { addSuffix: true });
}

function phaseTone(status?: string): "default" | "secondary" | "outline" | "destructive" {
  const normalized = String(status || "").toLowerCase();
  if (["authenticated", "ready", "completed", "restored", "linked", "search_ready"].includes(normalized)) return "default";
  if (["blocked_user_action", "blocked_approval", "failed", "permission_required"].includes(normalized)) return "destructive";
  if (["running", "refresh_due", "needs_processing", "people_csv_ready_for_processing"].includes(normalized)) return "secondary";
  return "outline";
}

function statusLabel(status?: string): string {
  const normalized = String(status || "unknown").toLowerCase();
  const labels: Record<string, string> = {
    blocked_approval: "approval needed",
    blocked_user_action: "action needed",
    needs_processing: "update available",
    people_csv_ready_for_processing: "update available",
    permission_required: "permission needed",
    refresh_due: "ready",
    search_ready: "ready",
  };
  return (labels[normalized] || normalized).replace(/_/g, " ");
}

function StatusBadge({ status }: { status?: string }) {
  return (
    <Badge variant={phaseTone(status)} className="capitalize">
      {statusLabel(status)}
    </Badge>
  );
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

function money(value?: number | null): string {
  if (typeof value !== "number" || Number.isNaN(value)) return "$0.00";
  return `$${value.toFixed(2)}`;
}

function paidCallTotal(value?: Record<string, number>): number {
  if (!value) return 0;
  return Object.values(value).reduce((total, count) => total + (Number(count) || 0), 0);
}

function stringList(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.map(String).filter(Boolean);
}

function linkedLabels(source: SetupSourceStatus): string[] {
  if (source.id === "gmail") {
    const selected = stringList(source.config.selected_accounts);
    const accountEmails = stringList(source.config.account_emails);
    return selected.length ? selected : accountEmails.length ? accountEmails : source.usernames;
  }
  return source.usernames.length ? source.usernames : source.artifacts;
}

function Metric({ label, value }: { label: string; value?: string | number | null }) {
  if (value == null || value === "") return null;
  return (
    <div className="min-w-0 rounded-md border bg-background px-3 py-2">
      <div className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">{label}</div>
      <div className="mt-1 truncate text-sm font-semibold">{typeof value === "number" ? value.toLocaleString() : value}</div>
    </div>
  );
}

function sourceStatusSummary(source: SetupSourceStatus): string {
  if (source.linked) return "Linked";
  if (source.skipped) return "Skipped";
  return statusLabel(source.status || "available");
}

function SourceChecklist({ sources }: { sources: SetupSourceStatus[] }) {
  return (
    <div className="grid gap-2 md:grid-cols-2 xl:grid-cols-4">
      {sources.map((source) => {
        const labels = linkedLabels(source);
        const ready = source.linked || source.skipped;
        return (
          <div key={source.id} className="rounded-md border bg-background p-3">
            <div className="flex items-center justify-between gap-2">
              <div className="truncate text-sm font-medium">{source.label}</div>
              {ready ? (
                <CheckCircle2 className="h-4 w-4 shrink-0 text-emerald-600" />
              ) : (
                <CircleDot className="h-4 w-4 shrink-0 text-muted-foreground" />
              )}
            </div>
            <div className="mt-2">
              <StatusBadge status={source.linked ? "linked" : source.skipped ? "skipped" : source.status} />
            </div>
            <div className="mt-2 truncate text-xs text-muted-foreground">
              {labels.length ? labels.slice(0, 2).join(", ") : sourceStatusSummary(source)}
            </div>
          </div>
        );
      })}
    </div>
  );
}

function jobSummary(job?: SetupJob | null): string {
  if (!job) return "";
  const output = job.output || {};
  const payload = (output.payload || output) as Record<string, unknown>;
  const message = String(payload.message || payload.reason || output.message || output.reason || "").trim();
  if (message) return message.replace(/\.powerpacks\/[^\s",}]+\.json/g, "local state file");
  if (job.status === "running") return `Started ${updatedLabel(job.startedAt) || "now"}`;
  if (job.completedAt) return `Finished ${updatedLabel(job.completedAt)}`;
  return "No output yet.";
}

function ActiveJobPanel({ job }: { job?: SetupJob | null }) {
  if (!job) return null;
  return (
    <section className="rounded-md border bg-card p-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex min-w-0 items-center gap-2">
          {job.status === "running" ? <Loader2 className="h-4 w-4 animate-spin text-muted-foreground" /> : <Clock3 className="h-4 w-4 text-muted-foreground" />}
          <div className="min-w-0">
            <div className="truncate text-sm font-medium">{job.action}</div>
            <div className="truncate text-xs text-muted-foreground">{jobSummary(job)}</div>
          </div>
        </div>
        <StatusBadge status={job.status} />
      </div>
    </section>
  );
}

interface GuideStepProps {
  index: number;
  title: string;
  status: string;
  description: string;
  selected?: boolean;
  icon: typeof Link2;
  metrics?: Array<{ label: string; value?: string | number | null }>;
  children?: ReactNode;
}

function GuideStep({ index, title, status, description, selected, icon: Icon, metrics = [], children }: GuideStepProps) {
  return (
    <section className={cn("rounded-md border bg-card p-4", selected && "border-primary/60 bg-primary/5")}>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="flex min-w-0 items-start gap-3">
          <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-md border bg-background">
            <Icon className="h-4 w-4 text-muted-foreground" />
          </div>
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <Badge variant="outline">{index}</Badge>
              <h3 className="text-base font-semibold">{title}</h3>
              <StatusBadge status={status} />
            </div>
            <p className="mt-1 text-sm text-muted-foreground">{description}</p>
          </div>
        </div>
      </div>
      {metrics.length > 0 && (
        <div className="mt-4 grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
          {metrics.map((metric) => (
            <Metric key={metric.label} label={metric.label} value={metric.value} />
          ))}
        </div>
      )}
      {children && <div className="mt-4">{children}</div>}
    </section>
  );
}

function recommendedStep(status: SetupStatusResponse): "link" | "import" | "enrichment" | "index" | "ready" {
  const hasLinkedSource = status.accounts.sources.some((source) => source.linked && !source.skipped);
  const importReady = status.import.sources.some((source) => source.linked && !source.skipped && source.runnable !== false);
  const indexReadiness = String(status.index.readiness || "").toLowerCase();
  if (!hasLinkedSource) return "link";
  if (status.messages.currentBlock || importReady || ["refresh_due", "running"].includes(String(status.import.status || "").toLowerCase())) return "import";
  if (status.enrichment.totalCandidates > status.enrichment.totalEnriched) return "enrichment";
  if (!["ready", "search_ready"].includes(indexReadiness)) return "index";
  return "ready";
}

export function LocalOnboardingPage({ onOpenSetupTab, onOpenMessagesReview }: LocalOnboardingPageProps) {
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
      setError(err instanceof Error ? err.message : "Failed to load onboarding status");
    } finally {
      setIsLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

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

  if (isLoading && !status) {
    return (
      <div className="flex min-h-[60vh] items-center justify-center gap-2 text-muted-foreground">
        <Loader2 className="h-5 w-5 animate-spin" /> Loading onboarding
      </div>
    );
  }

  if (!status) {
    return (
      <div className="rounded-md border bg-card p-6 text-sm text-muted-foreground">
        Onboarding status is unavailable.
      </div>
    );
  }

  const recommendation = recommendedStep(status);
  const estimate = status.index.processingEstimate || {};
  const paidCalls = paidCallTotal(estimate.estimatedPaidCalls);
  const currentBlock = status.messages.currentBlock || null;
  const blockMessage = String(currentBlock?.message || "").trim();

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="text-2xl font-semibold">Onboarding</h2>
          <div className="mt-1 flex flex-wrap items-center gap-2 text-sm text-muted-foreground">
            {status.operator.email && <span>{status.operator.email}</span>}
            {status.setup.updatedAt && <span>Updated {updatedLabel(status.setup.updatedAt)}</span>}
          </div>
        </div>
        <div className="flex flex-wrap gap-2">
          <Button variant="outline" size="sm" onClick={refresh} disabled={running}>
            <RefreshCcw className={cn("h-4 w-4", running && "animate-spin")} /> Refresh
          </Button>
          <Button variant="outline" size="sm" onClick={() => onOpenSetupTab("link")}>
            <Link2 className="h-4 w-4" /> Detailed Setup
          </Button>
        </div>
      </div>

      {error && (
        <div className="rounded-md border border-destructive/40 bg-destructive/5 px-3 py-2 text-sm text-destructive">
          {error}
        </div>
      )}

      {currentBlock && (
        <section className="rounded-md border border-amber-300 bg-amber-50 p-4 text-amber-950">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div className="min-w-0">
              <div className="flex items-center gap-2">
                <CircleAlert className="h-4 w-4" />
                <h3 className="text-base font-semibold">Action needed</h3>
              </div>
              <p className="mt-1 text-sm">{blockMessage || "A setup step is waiting for your input."}</p>
            </div>
            <div className="flex flex-wrap gap-2">
              <Button size="sm" onClick={onOpenMessagesReview}>
                <MessageSquare className="h-4 w-4" /> Manage Review
              </Button>
              <Button size="sm" variant="outline" onClick={() => runAction({ action: "messages-complete-review" })}>
                <CheckCircle2 className="h-4 w-4" /> Complete
              </Button>
            </div>
          </div>
        </section>
      )}

      <ActiveJobPanel job={activeJob} />

      <GuideStep
        index={1}
        title="Restore Previous Progress"
        status={status.bootstrap.status || status.setup.phases.bootstrap}
        description="Use any local bootstrap bundle that was already synced by Powerset setup."
        icon={UploadCloud}
        metrics={[
          { label: "People", value: status.bootstrap.peopleRecords || 0 },
          { label: "LinkedIn", value: status.bootstrap.linkedinCount || 0 },
          { label: "Twitter/X", value: status.bootstrap.twitterCount || 0 },
          { label: "Companies", value: status.bootstrap.companyRecords || 0 },
        ]}
      >
        <Button size="sm" onClick={() => runAction({ action: "bootstrap" })} disabled={running}>
          <Play className="h-4 w-4" /> Check Bootstrap
        </Button>
      </GuideStep>

      <GuideStep
        index={2}
        title="Connect Sources"
        status={status.accounts.linkedSources.length ? "linked" : status.setup.phases.link}
        description="Link Gmail, LinkedIn CSV, Messages, WhatsApp, or Twitter. Empty sources are optional."
        icon={Link2}
        selected={recommendation === "link"}
        metrics={[
          { label: "Linked", value: status.accounts.linkedSources.length },
          { label: "Skipped", value: status.accounts.skippedSources.length },
          { label: "Available", value: status.accounts.unresolvedSources.length },
        ]}
      >
        <div className="space-y-3">
          <SourceChecklist sources={status.accounts.sources} />
          <Button size="sm" onClick={() => onOpenSetupTab("link")}>
            <Link2 className="h-4 w-4" /> Link Accounts
          </Button>
        </div>
      </GuideStep>

      <GuideStep
        index={3}
        title="Import Linked Sources"
        status={status.import.status}
        description="Refresh connected sources, reuse existing local artifacts, then merge the import outputs."
        icon={Database}
        selected={recommendation === "import"}
        metrics={[
          { label: "Ready", value: status.import.sources.filter((source) => source.linked && !source.skipped && source.runnable !== false).length },
          { label: "Sources", value: status.import.sources.length },
          { label: "Last import", value: updatedLabel(status.import.updatedAt) },
          { label: "Messages review", value: status.review.counts.total },
        ]}
      >
        <div className="flex flex-wrap gap-2">
          <Button size="sm" onClick={() => runAction({ action: "import" })} disabled={running}>
            <Play className="h-4 w-4" /> Run Import
          </Button>
          <Button size="sm" variant="outline" onClick={() => onOpenSetupTab("import")}>
            Import Details
          </Button>
          <Button size="sm" variant="outline" onClick={onOpenMessagesReview} disabled={!status.review.exists && status.review.counts.total === 0}>
            <MessageSquare className="h-4 w-4" /> Review Messages
          </Button>
        </div>
      </GuideStep>

      <GuideStep
        index={4}
        title="Enrich People"
        status={status.enrichment.status}
        description="Find profile data for approved imported people and merge profiles back into people.csv."
        icon={Sparkles}
        selected={recommendation === "enrichment"}
        metrics={[
          { label: "To enrich", value: status.enrichment.totalCandidates },
          { label: "Profiles", value: status.enrichment.totalEnriched },
          { label: "Sources", value: status.enrichment.sources.length },
        ]}
      >
        <div className="flex flex-wrap gap-2">
          <Button size="sm" onClick={() => runAction({ action: "enrich-all" })} disabled={running}>
            <Sparkles className="h-4 w-4" /> Run Enrichment
          </Button>
          <Button size="sm" variant="outline" onClick={() => onOpenSetupTab("enrichment")}>
            Enrichment Details
          </Button>
        </div>
      </GuideStep>

      <GuideStep
        index={5}
        title="Build Local Search"
        status={status.index.readiness || status.setup.phases.index}
        description="Build or update the local DuckDB search index from the clean people.csv."
        icon={HardDrive}
        selected={recommendation === "index"}
        metrics={[
          { label: "People", value: status.index.peopleRecords || 0 },
          { label: "DuckDB", value: formatBytes(status.index.duckdbSizeBytes) },
          { label: "Cost", value: money(estimate.totalEstimatedUsd) },
          { label: "Paid calls", value: paidCalls },
        ]}
      >
        <div className="flex flex-wrap gap-2">
          <Button size="sm" onClick={() => runAction({ action: "index" })} disabled={running}>
            <HardDrive className="h-4 w-4" /> {paidCalls > 0 || (estimate.totalEstimatedUsd || 0) > 0 ? "Approve & Update" : "Update Index"}
          </Button>
          <Button size="sm" variant="outline" onClick={() => onOpenSetupTab("index")}>
            Index Details
          </Button>
        </div>
      </GuideStep>
    </div>
  );
}
