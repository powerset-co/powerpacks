import { useQuery } from "@tanstack/react-query"
import { useMemo } from "react"

import { fetchSearchRun } from "@/lib/api/searches"
import type { SearchResult } from "@/types/searches"

export function searchRunKey(runId: string | null) {
  return ["searches", "run", runId] as const
}

/** One saved run; idle while no run is picked. Read again whenever its pane opens (the cached
 *  one shows meanwhile): a search can finish while its pane is closed. The 2 s keeps the pane's
 *  mount after the fade from reading it a second time. */
export function useSearchRun(runId: string | null) {
  return useQuery({
    queryKey: searchRunKey(runId),
    queryFn: ({ signal }) => fetchSearchRun(runId ?? "", signal),
    enabled: runId !== null,
    staleTime: 2_000,
  })
}

/** Each person's score on the step the showing one came from, read once and kept under the run's key;
 *  null on the original search, empty while the parent step loads. */
export function useParentScores(search: SearchResult): ReadonlyMap<string, number> | null {
  const parent = search.steps.find((step) => step.id === search.current_step)?.parent ?? null
  const run = useQuery({
    queryKey: [...searchRunKey(search.run_id), "step", parent],
    queryFn: ({ signal }) => fetchSearchRun(search.run_id, signal, parent ?? undefined),
    enabled: parent !== null,
    // A step is a saved file that never changes.
    staleTime: Infinity,
  })
  return useMemo(() => {
    if (parent === null) return null
    const scores = new Map<string, number>()
    if (!run.data) return scores
    for (const pond of run.data.search.ponds)
      for (const row of pond.candidates)
        if (!scores.has(row.person_id)) scores.set(row.person_id, row.final_score)
    return scores
  }, [parent, run.data])
}
