import { useQuery } from "@tanstack/react-query"

import { setupDone } from "@/lib/api/install"
import { isDesktop } from "@/lib/desktop"

// Asked again every few seconds, so Setup leaves the side nav once a run finishes.
const POLL_MS = 5_000

/** Whether the desktop app's setup is done (run here, skipped, or imported). False in a browser,
 *  which has no app to ask. */
export function useSetupDone(): boolean {
  const query = useQuery({
    queryKey: ["setup-done"],
    queryFn: setupDone,
    enabled: isDesktop(),
    refetchInterval: POLL_MS,
  })
  return query.data === true
}
