import { useCallback, useEffect, useRef, useState } from "react";
import { Loader2, Search } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { fetchLocalSearchJob, startLocalSearch } from "./localSearchApi";
import type { LocalSearchJob } from "./localSearchApi";

const POLL_INTERVAL_MS = 2000;

const STEP_LABELS: Record<string, string> = {
  resolve_companies: "Resolving companies",
  resolve_education: "Resolving schools",
  apply_prefilters: "Applying filters",
  execute_role_search: "Searching local index",
  hydrate_people: "Hydrating profiles",
  llm_filter_candidates: "LLM filtering candidates",
  llm_rerank_candidates: "LLM reranking",
  persist_search_results: "Exporting results",
};

function progressLabel(job: LocalSearchJob | null): string {
  if (!job) return "Starting local search...";
  const visible = job.steps.filter((step) => STEP_LABELS[step.id]);
  const running = visible.find((step) => step.status === "running");
  if (running) return `${STEP_LABELS[running.id]}...`;
  const completed = visible.filter((step) => step.status === "completed");
  if (completed.length > 0) return `${STEP_LABELS[completed[completed.length - 1].id]} done...`;
  return "Expanding query (OpenAI)...";
}

interface LocalSearchLauncherProps {
  /** Increment to clear the query and focus the input (New Search). */
  focusToken?: number;
}

export function LocalSearchLauncher({ focusToken = 0 }: LocalSearchLauncherProps) {
  const [query, setQuery] = useState("");
  const [jobId, setJobId] = useState<string | null>(null);
  const [job, setJob] = useState<LocalSearchJob | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const pollRef = useRef<number | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    if (!focusToken) return;
    setQuery("");
    setError(null);
    inputRef.current?.focus();
  }, [focusToken]);

  const stopPolling = useCallback(() => {
    if (pollRef.current != null) {
      window.clearInterval(pollRef.current);
      pollRef.current = null;
    }
  }, []);

  useEffect(() => stopPolling, [stopPolling]);

  useEffect(() => {
    if (!jobId) return;
    stopPolling();

    const poll = async () => {
      let next: LocalSearchJob;
      try {
        next = await fetchLocalSearchJob(jobId);
      } catch (err) {
        stopPolling();
        setJobId(null);
        setError(err instanceof Error ? err.message : "Failed to poll local search job");
        return;
      }
      setJob(next);
      if (next.status === "running") return;

      stopPolling();
      setJobId(null);
      if (next.status === "completed") {
        const target = next.conversationId || next.taskId;
        if (target) {
          window.location.href = `/conversation/${encodeURIComponent(target)}`;
        } else {
          setError("Search finished but no run was found. Try refreshing runs.");
        }
        return;
      }
      setError(next.error || "Local search failed");
    };

    poll();
    pollRef.current = window.setInterval(poll, POLL_INTERVAL_MS);
    return stopPolling;
  }, [jobId, stopPolling]);

  const isRunning = submitting || jobId != null;

  const handleSubmit = async (event: React.FormEvent) => {
    event.preventDefault();
    const trimmed = query.trim();
    if (!trimmed || isRunning) return;
    setError(null);
    setJob(null);
    setSubmitting(true);
    try {
      const started = await startLocalSearch(trimmed);
      setJobId(started.jobId);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to start local search");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Card>
      <CardContent className="space-y-2 py-4">
        <form onSubmit={handleSubmit} className="flex flex-wrap items-center gap-2">
          <div className="relative min-w-0 flex-1">
            <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
            <Input
              ref={inputRef}
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="Search your local index, e.g. software engineers in SF"
              className="pl-9"
              disabled={isRunning}
              aria-label="Local search query"
            />
          </div>
          <Button type="submit" disabled={isRunning || !query.trim()}>
            {isRunning ? (
              <span className="inline-flex items-center gap-2">
                <Loader2 className="h-4 w-4 animate-spin" /> Searching...
              </span>
            ) : (
              "Search local index"
            )}
          </Button>
        </form>

        <p className="text-xs text-muted-foreground">
          Retrieval runs fully on the local index (no TurboPuffer or Postgres). Each search uses
          ~1 OpenAI query-expansion call, plus LLM filter/rerank over the local candidates.
        </p>

        {isRunning && (
          <p className="inline-flex items-center gap-2 text-sm text-muted-foreground">
            <Loader2 className="h-3.5 w-3.5 animate-spin" /> {progressLabel(job)}
          </p>
        )}

        {error && (
          <div className="space-y-2 rounded-md border border-destructive/40 bg-destructive/5 p-3">
            <p className="text-sm text-destructive">{error}</p>
            {job?.logTail ? (
              <pre className="max-h-40 overflow-auto whitespace-pre-wrap rounded bg-muted/50 p-2 text-xs text-muted-foreground">
                {job.logTail}
              </pre>
            ) : null}
          </div>
        )}
      </CardContent>
    </Card>
  );
}
