import { useQuery } from "@tanstack/react-query"

import { fetchBadResults, fetchSearchRun } from "@/lib/api/searches"

export function searchRunKey(runId: string | null) {
  return ["searches", "run", runId] as const
}

const POLL_MS = 2_000

/** One saved run; idle while no run is picked. Read again whenever its pane opens (the cached
 *  one shows meanwhile): a search can finish while its pane is closed. The 2 s keeps the pane's
 *  mount after the fade from reading it a second time. A run showing only its first people is
 *  read again until the rest are in. */
export function useSearchRun(runId: string | null) {
  return useQuery({
    queryKey: searchRunKey(runId),
    queryFn: ({ signal }) => fetchSearchRun(runId ?? "", signal),
    enabled: runId !== null,
    staleTime: 2_000,
    refetchInterval: (query) => (query.state.data?.search.partial ? POLL_MS : false),
  })
}

/** The people the run's Powerset search turned down, read only once asked for and again until
 *  their download is in. */
export function useBadResults(runId: string, asked: boolean) {
  return useQuery({
    queryKey: ["searches", "bad", runId] as const,
    queryFn: ({ signal }) => fetchBadResults(runId, signal),
    enabled: asked,
    staleTime: Infinity,
    refetchInterval: (query) => (query.state.data?.status === "loading" ? POLL_MS : false),
  })
}
