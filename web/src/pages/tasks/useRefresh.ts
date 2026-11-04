import { useQuery } from "@tanstack/react-query"

import { fetchRefresh, REFRESH_KEY } from "@/lib/api/refresh"

// While a run is going its row and the sidebar follow it; otherwise a daily run is noticed within a minute.
const RUNNING_POLL_MS = 3_000
const IDLE_POLL_MS = 60_000

/** The daily refresh's state, shared by Scheduled tasks and the sidebar's pane. */
export function useRefresh() {
  return useQuery({
    queryKey: REFRESH_KEY,
    queryFn: ({ signal }) => fetchRefresh(signal),
    refetchInterval: (query) => (query.state.data?.running ? RUNNING_POLL_MS : IDLE_POLL_MS),
    retry: false,
  })
}
