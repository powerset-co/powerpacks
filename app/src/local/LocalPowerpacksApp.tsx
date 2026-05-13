import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { format } from "date-fns";
import { Loader2, RefreshCcw } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { TooltipProvider } from "@/components/ui/tooltip";
import { fetchRunResults, fetchRuns } from "./powerpacksApi";
import { LocalQueryExpansionPanel } from "./LocalQueryExpansionPanel";
import { LocalResultsTable } from "./LocalResultsTable";
import { LocalRunSidebar } from "./LocalRunSidebar";
import type { LocalRunResultsResponse, LocalRunSummary } from "./types";
import { toDatabaseRecord } from "./types";

const PAGE_SIZE = 50;

function taskIdFromPath(): string | null {
  const match = window.location.pathname.match(/^\/conversation\/([^/]+)/);
  return match ? decodeURIComponent(match[1]) : null;
}

function setConversationPath(taskId: string) {
  const nextPath = `/conversation/${encodeURIComponent(taskId)}`;
  if (window.location.pathname !== nextPath) {
    window.history.pushState({}, "", nextPath);
  }
}

function mergeResults(
  previous: LocalRunResultsResponse | null,
  next: LocalRunResultsResponse,
  append: boolean
): LocalRunResultsResponse {
  if (!append || !previous) return next;

  const seen = new Set(previous.rows.map((row) => String(row.person_id || row.linkedin_url || row.name || "")));
  const appendedRows = next.rows.filter((row) => {
    const key = String(row.person_id || row.linkedin_url || row.name || "");
    if (!key || seen.has(key)) return false;
    seen.add(key);
    return true;
  });

  return {
    ...next,
    rows: [...previous.rows, ...appendedRows],
    profiles: { ...previous.profiles, ...next.profiles },
  };
}

export function LocalPowerpacksApp() {
  const [runs, setRuns] = useState<LocalRunSummary[]>([]);
  const [runsLoading, setRunsLoading] = useState(true);
  const [search, setSearch] = useState("");
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(() => taskIdFromPath());
  const [resultResponse, setResultResponse] = useState<LocalRunResultsResponse | null>(null);
  const [resultsLoading, setResultsLoading] = useState(false);
  const [isLoadingMore, setIsLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const sentinelRef = useRef<HTMLDivElement | null>(null);

  const refreshRuns = async () => {
    setRunsLoading(true);
    setError(null);
    try {
      const nextRuns = await fetchRuns();
      setRuns(nextRuns);
      setSelectedTaskId((current) => current || nextRuns.find((run) => run.hasArtifacts)?.conversationId || nextRuns.find((run) => run.hasArtifacts)?.taskId || nextRuns[0]?.conversationId || nextRuns[0]?.taskId || null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load runs");
    } finally {
      setRunsLoading(false);
    }
  };

  const loadResultsPage = useCallback(async (offset: number, append: boolean) => {
    if (!selectedTaskId) return;
    if (append) setIsLoadingMore(true);
    else setResultsLoading(true);
    setError(null);
    try {
      const response = await fetchRunResults(selectedTaskId, { offset, limit: PAGE_SIZE });
      setResultResponse((previous) => mergeResults(previous, response, append));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load results");
    } finally {
      if (append) setIsLoadingMore(false);
      else setResultsLoading(false);
    }
  }, [selectedTaskId]);

  useEffect(() => {
    refreshRuns();

    const handlePopState = () => setSelectedTaskId(taskIdFromPath());
    window.addEventListener("popstate", handlePopState);
    return () => window.removeEventListener("popstate", handlePopState);
  }, []);

  useEffect(() => {
    if (!selectedTaskId) return;
    setResultResponse(null);
    loadResultsPage(0, false);
  }, [selectedTaskId, loadResultsPage]);

  useEffect(() => {
    const sentinel = sentinelRef.current;
    if (!sentinel || !resultResponse?.hasMore || resultsLoading || isLoadingMore) return;

    const observer = new IntersectionObserver((entries) => {
      if (entries.some((entry) => entry.isIntersecting)) {
        loadResultsPage(resultResponse.rows.length, true);
      }
    }, { rootMargin: "600px" });

    observer.observe(sentinel);
    return () => observer.disconnect();
  }, [resultResponse?.hasMore, resultResponse?.rows.length, resultsLoading, isLoadingMore, loadResultsPage]);

  const filteredRuns = useMemo(() => {
    const q = search.trim().toLowerCase();
    if (!q) return runs;
    return runs.filter((run) => [run.query, run.taskId, run.status].some((value) => String(value || "").toLowerCase().includes(q)));
  }, [runs, search]);

  const records = useMemo(() => {
    if (!resultResponse) return [];
    return resultResponse.rows.map((row) => {
      const personId = String(row.person_id || "");
      return toDatabaseRecord(row, personId ? resultResponse.profiles[personId] : undefined);
    });
  }, [resultResponse]);

  const selectedRun = resultResponse?.run || runs.find((run) => run.taskId === selectedTaskId || run.conversationId === selectedTaskId);
  const totalResults = selectedRun?.rowCount ?? resultResponse?.totalRows ?? records.length;
  const loadedCount = resultResponse?.rows.length ?? 0;

  return (
    <TooltipProvider>
      <div className="flex min-h-dvh bg-background text-foreground">
        <LocalRunSidebar
          runs={filteredRuns}
          selectedTaskId={selectedTaskId}
          isLoading={runsLoading}
          search={search}
          onSearchChange={setSearch}
          onSelect={(run) => {
            const id = run.conversationId || run.taskId;
            setSelectedTaskId(id);
            setConversationPath(id);
          }}
        />

        <main className="min-w-0 flex-1 overflow-y-auto">
          <div className="mx-auto max-w-7xl space-y-4 p-6">
            <div className="flex flex-wrap items-start justify-between gap-4">
              <div className="min-w-0">
                <h2 className="truncate text-2xl font-semibold">{selectedRun?.query || "Select a search run"}</h2>
                {selectedRun?.updatedAt && (
                  <p className="mt-1 text-sm text-muted-foreground">
                    Updated {format(new Date(selectedRun.updatedAt), "MMM d, yyyy h:mm a")}
                  </p>
                )}
              </div>
              <Button variant="outline" size="sm" onClick={refreshRuns} disabled={runsLoading}>
                <RefreshCcw className="mr-2 h-4 w-4" /> Refresh runs
              </Button>
            </div>

            {error && (
              <Card className="border-destructive/40 bg-destructive/5">
                <CardContent className="py-3 text-sm text-destructive">{error}</CardContent>
              </Card>
            )}

            {selectedRun && <LocalQueryExpansionPanel run={selectedRun} />}

            {resultsLoading ? (
              <div className="flex items-center justify-center gap-2 rounded-lg border p-12 text-muted-foreground">
                <Loader2 className="h-5 w-5 animate-spin" /> Loading first {PAGE_SIZE} results...
              </div>
            ) : records.length > 0 ? (
              <>
                <LocalResultsTable
                  records={records}
                  query={selectedRun?.query}
                  conversationId={selectedRun?.conversationId || selectedTaskId}
                  totalCount={totalResults}
                />
                <div ref={sentinelRef} className="flex min-h-16 items-center justify-center py-4 text-sm text-muted-foreground">
                  {isLoadingMore ? (
                    <span className="inline-flex items-center gap-2"><Loader2 className="h-4 w-4 animate-spin" /> Loading more results...</span>
                  ) : resultResponse?.hasMore ? (
                    <Button variant="outline" size="sm" onClick={() => loadResultsPage(loadedCount, true)}>
                      Load more
                    </Button>
                  ) : (
                    <span>All loaded</span>
                  )}
                </div>
              </>
            ) : selectedRun ? (
              <Card>
                <CardContent className="py-10 text-center text-muted-foreground">
                  No result artifact found yet for this run.
                </CardContent>
              </Card>
            ) : (
              <Card>
                <CardContent className="py-10 text-center text-muted-foreground">
                  Select a run from the sidebar to view results.
                </CardContent>
              </Card>
            )}
          </div>
        </main>
      </div>
    </TooltipProvider>
  );
}
