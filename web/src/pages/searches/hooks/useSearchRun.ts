import { useQuery } from "@tanstack/react-query"

import { fetchSearchRun } from "@/lib/api/searches"

// A saved run changes only when the harness rewrites it; a pane swap re-reads the cache.
const RUN_STALE_MS = 60_000

/** One saved run; idle while no run is picked. */
export function useSearchRun(runId: string | null) {
  return useQuery({
    queryKey: ["searches", "run", runId],
    queryFn: ({ signal }) => fetchSearchRun(runId ?? "", signal),
    enabled: runId !== null,
    staleTime: RUN_STALE_MS,
  })
}
