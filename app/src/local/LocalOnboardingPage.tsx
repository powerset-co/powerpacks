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

function paidCallTotal(value?: Record<string, number>): number {
  if (!value) return 0;
  return Object.values(value).reduce((total, count) => total + (Number(count) || 0), 0);
}

function stringList(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.map(String).filter(Boolean);
}

function uniqueLabels(values: string[]): string[] {
  return Array.from(new Set(values.map((value) => value.trim()).filter(Boolean)));
}

function linkedLabels(source: SetupSourceStatus): string[] {
  if (source.id === "gmail") {
    const selected = stringList(source.config.selected_accounts);
    const accountEmails = stringList(source.config.account_emails);
    return uniqueLabels(selected.length ? selected : accountEmails.length ? accountEmails : source.usernames);
  }
  return uniqueLabels(source.usernames.length ? source.usernames : source.artifacts);
}

function sourceStatusSummary(source: SetupSourceStatus): string {
  if (source.linked) return "Linked";
  if (source.skipped) return "Skipped";
  return statusLabel(source.status || "available");
}

function SourceLabelPreview({ labels, fallback }: { labels: string[]; fallback: string }) {
  if (!labels.length) {
    return <div className="mt-2 text-xs text-muted-foreground">{fallback}</div>;
  }
  const visible = labels.slice(0, 4);
  const hiddenCount = Math.max(0, labels.length - visible.length);
  return (
    <div className="mt-2 flex max-h-24 flex-wrap gap-1.5 overflow-hidden">
      {visible.map((label) => (
        <Badge key={label} variant="secondary" className="max-w-full truncate" title={label}>
          {label}
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
            <SourceLabelPreview labels={labels} fallback={sourceStatusSummary(source)} />
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
  if (job.status === "running") {
    const line = [job.stdout, job.stderr]
      .filter(Boolean)
      .join("\n")
      .replace(/\.powerpacks\/[^\s",}]+\.json/g, "local state file")
      .split(/\r?\n/)
      .map((item) => item.trim())
      .filter(Boolean)
      .slice(-1)[0];
    if (line) return line;
  }
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
  title: string;
  description: string;
  icon: typeof Link2;
  children?: ReactNode;
}

function GuideStep({ title, description, icon: Icon, children }: GuideStepProps) {
  return (
    <section className="rounded-md border bg-card p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="flex min-w-0 items-start gap-3">
          <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-md border bg-background">
            <Icon className="h-4 w-4 text-muted-foreground" />
          </div>
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <h3 className="text-base font-semibold">{title}</h3>
            </div>
            <p className="mt-1 text-sm text-muted-foreground">{description}</p>
          </div>
        </div>
      </div>
      {children && <div className="mt-4">{children}</div>}
    </section>
  );
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

  const estimate = status.index.processingEstimate || {};
  const paidCalls = paidCallTotal(estimate.estimatedPaidCalls);
  const currentBlock = status.messages.currentBlock || null;
  const pendingReview = Number(status.review.counts.undecided || 0);
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

      {(currentBlock || pendingReview > 0) && (
        <section className="rounded-md border border-amber-300 bg-amber-50 p-4 text-amber-950">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div className="min-w-0">
              <div className="flex items-center gap-2">
                <CircleAlert className="h-4 w-4" />
                <h3 className="text-base font-semibold">Action needed</h3>
              </div>
              <p className="mt-1 text-sm">
                {blockMessage || `${pendingReview.toLocaleString()} Messages contacts are pending review. Only explicitly included contacts are merged.`}
              </p>
            </div>
            <div className="flex flex-wrap gap-2">
              <Button size="sm" onClick={onOpenMessagesReview}>
                <MessageSquare className="h-4 w-4" /> Manage Review
              </Button>
              {currentBlock && (
                <Button size="sm" variant="outline" onClick={() => runAction({ action: "messages-complete-review" })}>
                  <CheckCircle2 className="h-4 w-4" /> Complete
                </Button>
              )}
            </div>
          </div>
        </section>
      )}

      <ActiveJobPanel job={activeJob} />

      <GuideStep
        title="Connect Sources"
        description="Link Gmail, LinkedIn CSV, Messages, WhatsApp, or Twitter. Empty sources are optional."
        icon={Link2}
      >
        <div className="space-y-3">
          <SourceChecklist sources={status.accounts.sources} />
          <Button size="sm" onClick={() => onOpenSetupTab("link")}>
            <Link2 className="h-4 w-4" /> Link Accounts
          </Button>
        </div>
      </GuideStep>

      <GuideStep
        title="Import Linked Sources"
        description="Refresh connected sources, reuse existing local artifacts, then merge the import outputs."
        icon={Database}
      >
        <div className="flex flex-wrap gap-2">
          <Button size="sm" onClick={() => runAction({ action: "import" })} disabled={running}>
            <Play className="h-4 w-4" /> Run Import
          </Button>
          <Button size="sm" variant="outline" onClick={onOpenMessagesReview} disabled={!status.review.exists && status.review.counts.total === 0}>
            <MessageSquare className="h-4 w-4" /> Review Messages
          </Button>
        </div>
      </GuideStep>

      <GuideStep
        title="Enrich People"
        description="Find profile data for approved imported people and merge profiles back into people.csv."
        icon={Sparkles}
      >
        <div className="flex flex-wrap gap-2">
          <Button size="sm" onClick={() => runAction({ action: "enrich-all" })} disabled={running}>
            <Sparkles className="h-4 w-4" /> Run Enrichment
          </Button>
        </div>
      </GuideStep>

      <GuideStep
        title="Build Local Search"
        description="Build or update the local DuckDB search index from the clean people.csv."
        icon={HardDrive}
      >
        <div className="flex flex-wrap gap-2">
          <Button size="sm" onClick={() => runAction({ action: "index" })} disabled={running}>
            <HardDrive className="h-4 w-4" /> {paidCalls > 0 || (estimate.totalEstimatedUsd || 0) > 0 ? "Approve & Update" : "Update Index"}
          </Button>
        </div>
      </GuideStep>
    </div>
  );
}
