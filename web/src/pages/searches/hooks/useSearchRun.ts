import { useQuery } from "@tanstack/react-query"

import { fetchSearchRun } from "@/lib/api/searches"

export function searchRunKey(runId: string | null) {
  return ["searches", "run", runId] as const
}

/** One saved run; idle while no run is picked. Read again whenever its pane opens (the cached
 *  one shows meanwhile): a search can finish while its pane is closed. */
export function useSearchRun(runId: string | null) {
  return useQuery({
    queryKey: searchRunKey(runId),
    queryFn: ({ signal }) => fetchSearchRun(runId ?? "", signal),
    enabled: runId !== null,
  })
}
