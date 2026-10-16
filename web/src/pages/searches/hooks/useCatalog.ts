import { useQuery } from "@tanstack/react-query"

import { fetchCatalog } from "@/lib/api/searches"

// While the agent runs, asked again every few seconds: a search lists its screened people
// (status "running") before its ranking finishes.
const POLL_MS = 3_000

/** The saved searches, newest first (the server's order). */
export function useCatalog(agentRunning = false) {
  return useQuery({
    queryKey: ["searches", "catalog"],
    queryFn: async ({ signal }) => (await fetchCatalog(signal)).searches,
    refetchInterval: agentRunning ? POLL_MS : false,
  })
}
