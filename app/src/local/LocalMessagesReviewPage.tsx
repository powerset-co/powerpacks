import { useCallback, useEffect, useMemo, useState } from "react";
import { formatDistanceToNow } from "date-fns";
import {
  ArrowLeft,
  Check,
  CheckCircle2,
  CircleSlash,
  ExternalLink,
  Loader2,
  MessageSquare,
  RefreshCcw,
  Search,
  Sparkles,
  UploadCloud,
} from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";
import {
  bulkToggleMessageReview,
  fetchMessageReview,
  fetchSetupStatus,
  runSetupAction,
  saveMessageReviewHint,
  toggleMessageReviewRow,
} from "./powerpacksApi";
import type {
  MessageReviewFilter,
  MessageReviewResponse,
  MessageReviewRow,
  SetupJob,
  SetupStatusResponse,
} from "./types";

const REVIEW_PAGE_SIZE = 100;

const REVIEW_FILTERS: Array<{ id: MessageReviewFilter; label: string; countKey?: keyof MessageReviewResponse["counts"] }> = [
  { id: "all", label: "All", countKey: "total" },
  { id: "included", label: "Included", countKey: "included" },
  { id: "in_network", label: "In Network", countKey: "inNetwork" },
  { id: "yes", label: "Yes", countKey: "yes" },
  { id: "maybe", label: "Maybe", countKey: "maybe" },
  { id: "no", label: "No", countKey: "no" },
  { id: "feedback", label: "Feedback", countKey: "retargetFeedback" },
];

function stringValue(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function updatedLabel(value?: string | null): string {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return formatDistanceToNow(date, { addSuffix: true });
}

function tailText(value?: string, limit = 2200): string {
  const text = String(value || "");
  return text.length > limit ? text.slice(text.length - limit) : text;
}

function splitValue(value: string, limit = 4): string[] {
  return value.split("|").map((part) => part.trim()).filter(Boolean).slice(0, limit);
}

function phaseTone(status?: string): "default" | "secondary" | "outline" | "destructive" {
  const normalized = String(status || "").toLowerCase();
  if (["completed", "ok", "ready", "running"].includes(normalized)) return "default";
  if (["blocked_user_action", "blocked_approval", "failed"].includes(normalized)) return "destructive";
  if (["pending", "unknown", "selected_steps_completed"].includes(normalized)) return "secondary";
  return "outline";
}

function StatusBadge({ status }: { status?: string }) {
  const normalized = String(status || "unknown").toLowerCase();
  const labels: Record<string, string> = {
    blocked_approval: "approval needed",
    blocked_user_action: "action needed",
    selected_steps_completed: "ready",
  };
  return (
    <Badge variant={phaseTone(status)} className="capitalize">
      {(labels[normalized] || normalized).replace(/_/g, " ")}
    </Badge>
  );
}

function PillList({ label, value }: { label: string; value: string }) {
  const values = splitValue(value);
  if (!values.length) return null;
  return (
    <div className="space-y-1">
      <div className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">{label}</div>
      <div className="flex flex-wrap gap-1.5">
        {values.map((item) => (
          <Badge key={item} variant="secondary" className="max-w-full truncate">
            {item}
          </Badge>
        ))}
      </div>
    </div>
  );
}

function MetaLine({ label, value }: { label: string; value?: string | number | null }) {
  if (value == null || value === "") return null;
  return (
    <div className="min-w-0">
      <span className="font-medium text-foreground">{label}</span>{" "}
      <span className="break-words text-muted-foreground">{value}</span>
    </div>
  );
}

function jobSummary(job?: SetupJob | null): string {
  if (!job) return "";
  const output = job.output || {};
  const payload = (output.payload || output) as Record<string, unknown>;
  const message = stringValue(payload.message)
    || stringValue(payload.reason)
    || stringValue(output.message)
    || stringValue(output.reason);
  if (message) return message;
  if (job.status === "running") return `Started ${updatedLabel(job.startedAt) || "now"}`;
  if (job.completedAt) return `Finished ${updatedLabel(job.completedAt)}`;
  return "No output yet.";
}

function ReviewFilters({
  active,
  response,
  onChange,
}: {
  active: MessageReviewFilter;
  response: MessageReviewResponse | null;
  onChange: (filter: MessageReviewFilter) => void;
}) {
  return (
    <div role="tablist" aria-label="Review filters" className="flex flex-wrap gap-2">
      {REVIEW_FILTERS.map((filter) => {
        const selected = active === filter.id;
        const count = filter.countKey && response ? response.counts[filter.countKey] : undefined;
        return (
          <button
            key={filter.id}
            type="button"
            role="tab"
            aria-selected={selected}
            onClick={() => onChange(filter.id)}
            className={cn(
              "inline-flex h-9 items-center gap-2 rounded-md border px-3 text-sm font-medium transition-colors",
              selected ? "border-primary bg-primary/5 text-primary" : "border-input bg-background hover:bg-accent/10"
            )}
          >
            <span>{filter.label}</span>
            {typeof count === "number" && <Badge variant="secondary">{count.toLocaleString()}</Badge>}
          </button>
        );
      })}
    </div>
  );
}

function ReviewCard({
  row,
  draft,
  isSaving,
  onDecision,
  onDraftChange,
  onSaveHint,
}: {
  row: MessageReviewRow;
  draft: string;
  isSaving: boolean;
  onDecision: (row: MessageReviewRow, selected: boolean) => void;
  onDraftChange: (value: string) => void;
  onSaveHint: (row: MessageReviewRow) => void;
}) {
  const messageBits = [
    row.imessageMessages ? `iMessage ${row.imessageMessages.toLocaleString()}` : "",
    row.whatsappMessages ? `WhatsApp ${row.whatsappMessages.toLocaleString()}` : "",
  ].filter(Boolean);
  const linkedin = row.retargetLinkedInUrl || row.networkLinkedInUrl;
  const isRetargeted = Boolean(row.retargetStatus);
  const hasUnsavedHint = draft !== row.retargetHint;

  return (
    <article className={cn("rounded-md border bg-card p-4", row.selected ? "border-emerald-200" : "border-border opacity-80")}>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="truncate text-base font-semibold">{row.fullName || "Unknown"}</h3>
            {linkedin && (
              <a
                href={linkedin}
                target="_blank"
                rel="noreferrer"
                aria-label="Open LinkedIn profile"
                className="inline-flex h-5 w-5 items-center justify-center rounded bg-[#0A66C2] text-[10px] font-bold text-white"
              >
                in
              </a>
            )}
            {isRetargeted && <Badge variant="outline">re-researched</Badge>}
          </div>
          <div className="mt-1 flex flex-wrap items-center gap-2">
            <Badge variant={row.selected ? "default" : "secondary"}>
              {row.selected ? "Included" : "Excluded"}
            </Badge>
            <Badge variant="outline" className="capitalize">{row.tab.replace("_", " ")}</Badge>
            {row.reviewSource && <Badge variant="secondary">{row.reviewSource.replace(/_/g, " ")}</Badge>}
          </div>
        </div>
        <div className="flex flex-wrap gap-2">
          <Button size="sm" variant={row.selected ? "secondary" : "default"} onClick={() => onDecision(row, true)} disabled={isSaving || row.selected}>
            <Check className="h-4 w-4" /> Include
          </Button>
          <Button size="sm" variant={!row.selected ? "secondary" : "outline"} onClick={() => onDecision(row, false)} disabled={isSaving || !row.selected}>
            <CircleSlash className="h-4 w-4" /> Exclude
          </Button>
        </div>
      </div>

      <div className="mt-4 grid gap-3 text-sm lg:grid-cols-2">
        <MetaLine label="phone" value={row.phone || "unknown"} />
        <MetaLine label="messages" value={`${row.totalMessages.toLocaleString()}${messageBits.length ? ` (${messageBits.join(", ")})` : ""}`} />
        <MetaLine label="network" value={row.networkName || "none"} />
        <MetaLine label="match" value={row.networkMatchStatus || row.networkMatchConfidence} />
      </div>

      <div className="mt-4 grid gap-3 md:grid-cols-2">
        <PillList label="Groups" value={row.groupNames} />
        <PillList label="Signals" value={row.signals} />
      </div>

      <div className="mt-4 space-y-2 text-sm">
        <MetaLine label="title/company" value={row.titleCompanyPairs || "unknown"} />
        <MetaLine label="education" value={row.schools || "unknown"} />
        <MetaLine label="reason" value={row.shortReason || "none"} />
        <MetaLine label="identity" value={row.identityRisk || "none"} />
        {isRetargeted && <MetaLine label="latest result" value={row.retargetNotes || "merged from re-research"} />}
      </div>

      <div className="mt-4 space-y-2">
        <label className="text-xs font-medium text-muted-foreground" htmlFor={`hint-${row.index}`}>
          {isRetargeted ? "New feedback" : "Feedback"}
        </label>
        <textarea
          id={`hint-${row.index}`}
          value={draft}
          onChange={(event) => onDraftChange(event.target.value)}
          className="min-h-20 w-full resize-y rounded-md border bg-background px-3 py-2 text-sm outline-none transition-colors focus:border-primary"
          placeholder="LinkedIn URL, company, title, location, or clue"
        />
        <div className="flex flex-wrap items-center gap-2">
          <Button size="sm" variant="outline" onClick={() => onSaveHint(row)} disabled={isSaving || !hasUnsavedHint}>
            {isSaving ? <Loader2 className="h-4 w-4 animate-spin" /> : <Sparkles className="h-4 w-4" />}
            Save feedback
          </Button>
          {hasUnsavedHint && <span className="text-xs text-muted-foreground">Unsaved</span>}
        </div>
      </div>
    </article>
  );
}

export function LocalMessagesReviewPage({ onBackToSetup }: { onBackToSetup: () => void }) {
  const [filter, setFilter] = useState<MessageReviewFilter>("all");
  const [query, setQuery] = useState("");
  const [response, setResponse] = useState<MessageReviewResponse | null>(null);
  const [status, setStatus] = useState<SetupStatusResponse | null>(null);
  const [activeJobId, setActiveJobId] = useState<string | null>(null);
  const [drafts, setDrafts] = useState<Record<number, string>>({});
  const [savingRows, setSavingRows] = useState<Set<number>>(new Set());
  const [isLoading, setIsLoading] = useState(true);
  const [isLoadingMore, setIsLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const loadStatus = useCallback(async () => {
    const nextStatus = await fetchSetupStatus();
    setStatus(nextStatus);
  }, []);

  const loadReview = useCallback(async (offset = 0, append = false) => {
    if (append) setIsLoadingMore(true);
    else setIsLoading(true);
    setError(null);
    try {
      const next = await fetchMessageReview({ filter, query, offset, limit: REVIEW_PAGE_SIZE });
      setResponse((previous) => append && previous ? { ...next, rows: [...previous.rows, ...next.rows] } : next);
      setDrafts((current) => {
        const merged = { ...current };
        for (const row of next.rows) {
          if (merged[row.index] == null) merged[row.index] = row.retargetHint;
        }
        return merged;
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load review");
    } finally {
      setIsLoading(false);
      setIsLoadingMore(false);
    }
  }, [filter, query]);

  const refreshAll = useCallback(async () => {
    try {
      await Promise.all([loadReview(0, false), loadStatus()]);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to refresh review");
    }
  }, [loadReview, loadStatus]);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      loadReview(0, false);
    }, 200);
    return () => window.clearTimeout(timer);
  }, [loadReview]);

  useEffect(() => {
    loadStatus().catch((err) => setError(err instanceof Error ? err.message : "Failed to load setup status"));
  }, [loadStatus]);

  const activeJob = useMemo(() => {
    if (!status?.jobs.length) return null;
    return status.jobs.find((job) => job.id === activeJobId) || status.jobs[0];
  }, [activeJobId, status?.jobs]);
  const running = Boolean(status?.jobs.some((job) => job.status === "running"));

  useEffect(() => {
    if (!running) return undefined;
    const timer = window.setInterval(() => {
      loadStatus().catch(() => undefined);
    }, 2000);
    return () => window.clearInterval(timer);
  }, [loadStatus, running]);

  const runAction = async (body: Record<string, unknown>) => {
    setError(null);
    try {
      const next = await runSetupAction(body);
      setActiveJobId(next.job.id);
      await loadStatus();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to start messages action");
    }
  };

  const mutateRow = async (row: MessageReviewRow, fn: () => Promise<unknown>) => {
    setSavingRows((current) => new Set(current).add(row.index));
    setError(null);
    try {
      await fn();
      await loadReview(0, false);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to save review");
    } finally {
      setSavingRows((current) => {
        const next = new Set(current);
        next.delete(row.index);
        return next;
      });
    }
  };

  const currentBlock = status?.messages.currentBlock || null;
  const blockStatus = stringValue(currentBlock?.status);
  const approvalType = stringValue(currentBlock?.approval_type);
  const blockMessage = stringValue(currentBlock?.message);
  const reviewUrl = stringValue(currentBlock?.review_url);
  const updated = updatedLabel(response?.updatedAt);

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <Button variant="ghost" size="sm" className="-ml-2 mb-2" onClick={onBackToSetup}>
            <ArrowLeft className="h-4 w-4" /> Setup
          </Button>
          <div className="flex flex-wrap items-center gap-2">
            <h2 className="text-2xl font-semibold">Messages Review</h2>
            {status?.messages.status && <StatusBadge status={status.messages.status} />}
          </div>
          <div className="mt-1 flex flex-wrap items-center gap-2 text-sm text-muted-foreground">
            <span>{response?.counts.total.toLocaleString() || "0"} contacts</span>
            {updated && <span>Updated {updated}</span>}
          </div>
        </div>
        <div className="flex flex-wrap gap-2">
          <Button variant="outline" size="sm" onClick={refreshAll} disabled={isLoading || running}>
            <RefreshCcw className={cn("h-4 w-4", (isLoading || running) && "animate-spin")} /> Refresh
          </Button>
          {reviewUrl && (
            <Button variant="outline" size="sm" asChild>
              <a href={reviewUrl} target="_blank" rel="noreferrer">
                <ExternalLink className="h-4 w-4" /> Legacy Review
              </a>
            </Button>
          )}
          <Button size="sm" onClick={() => runAction({ action: "messages-complete-review" })} disabled={running}>
            <CheckCircle2 className="h-4 w-4" /> Complete
          </Button>
        </div>
      </div>

      {error && (
        <div className="rounded-md border border-destructive/40 bg-destructive/5 px-3 py-2 text-sm text-destructive">
          {error}
        </div>
      )}

      {currentBlock && (
        <section className="rounded-md border bg-card p-4">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div className="min-w-0">
              <div className="flex flex-wrap items-center gap-2">
                <MessageSquare className="h-4 w-4 text-muted-foreground" />
                <h3 className="text-base font-semibold">Messages pipeline</h3>
                <StatusBadge status={blockStatus} />
              </div>
              {blockMessage && <p className="mt-1 text-sm text-muted-foreground">{blockMessage}</p>}
            </div>
            {blockStatus === "blocked_approval" && (
              <Button size="sm" onClick={() => runAction({ action: "messages-approve-continue" })} disabled={running}>
                {approvalType === "upload" ? <UploadCloud className="h-4 w-4" /> : <Sparkles className="h-4 w-4" />}
                {approvalType === "upload" ? "Approve Upload" : "Approve and Continue"}
              </Button>
            )}
          </div>
        </section>
      )}

      {activeJob && (
        <section className="rounded-md border bg-card p-4">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="min-w-0">
              <div className="truncate text-sm font-medium">{activeJob.action}</div>
              <div className="truncate text-xs text-muted-foreground">{jobSummary(activeJob)}</div>
            </div>
            <StatusBadge status={activeJob.status} />
          </div>
          {(activeJob.stderr || activeJob.stdout) && (
            <details className="mt-3">
              <summary className="cursor-pointer text-xs font-medium text-muted-foreground">Logs</summary>
              <pre className="mt-2 max-h-60 overflow-auto whitespace-pre-wrap rounded-md border bg-muted/30 p-3 text-xs text-muted-foreground">
                {tailText([activeJob.stdout, activeJob.stderr].filter(Boolean).join("\n"))}
              </pre>
            </details>
          )}
        </section>
      )}

      <section className="rounded-md border bg-card p-4">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <ReviewFilters active={filter} response={response} onChange={setFilter} />
          <div className="relative w-full sm:w-80">
            <Search className="absolute left-2.5 top-2.5 h-4 w-4 text-muted-foreground" />
            <Input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search review" className="pl-8" />
          </div>
        </div>
        {filter === "in_network" && (
          <div className="mt-3 flex flex-wrap gap-2 border-t pt-3">
            <Button size="sm" variant="outline" onClick={() => runAction({ action: "messages-complete-review" })} disabled={running}>
              <CheckCircle2 className="h-4 w-4" /> Complete Review
            </Button>
            <Button size="sm" variant="outline" onClick={() => bulkToggleMessageReview("in_network", true).then(() => loadReview(0, false))}>
              Select all in-network
            </Button>
            <Button size="sm" variant="outline" onClick={() => bulkToggleMessageReview("in_network", false).then(() => loadReview(0, false))}>
              Select none in-network
            </Button>
          </div>
        )}
      </section>

      {isLoading && !response ? (
        <div className="flex min-h-56 items-center justify-center gap-2 rounded-md border text-muted-foreground">
          <Loader2 className="h-5 w-5 animate-spin" /> Loading review
        </div>
      ) : response && response.rows.length > 0 ? (
        <>
          <div className="grid gap-3 xl:grid-cols-2">
            {response.rows.map((row) => (
              <ReviewCard
                key={row.index}
                row={row}
                draft={drafts[row.index] ?? row.retargetHint}
                isSaving={savingRows.has(row.index)}
                onDecision={(target, selected) => mutateRow(target, () => toggleMessageReviewRow(target.index, selected))}
                onDraftChange={(value) => setDrafts((current) => ({ ...current, [row.index]: value }))}
                onSaveHint={(target) => mutateRow(target, () => saveMessageReviewHint(target.index, drafts[target.index] ?? target.retargetHint))}
              />
            ))}
          </div>
          <div className="flex min-h-14 items-center justify-center">
            {response.hasMore ? (
              <Button
                variant="outline"
                size="sm"
                disabled={isLoadingMore}
                onClick={() => loadReview(response.rows.length, true)}
              >
                {isLoadingMore ? <Loader2 className="h-4 w-4 animate-spin" /> : null}
                Load more
              </Button>
            ) : (
              <span className="text-sm text-muted-foreground">
                {response.filteredCount.toLocaleString()} shown
              </span>
            )}
          </div>
        </>
      ) : (
        <div className="rounded-md border bg-card p-8 text-center text-sm text-muted-foreground">
          No review rows match this view.
        </div>
      )}
    </div>
  );
}
