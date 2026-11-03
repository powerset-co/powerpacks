import { useQueries, type QueryClient } from "@tanstack/react-query"
import { useCallback, useMemo, useState } from "react"

import { fetchPeoplePage } from "@/lib/api/people"
import { viewQuery, type PeopleView } from "@/lib/people/view"
import type { PeoplePage, Person } from "@/types/people"

/** Every slice of every view lives under this key; writes patch and refetch them all. */
export const PEOPLE_QUERY_KEY = ["people"] as const

// Rows per request: a few screens, so a scroll rarely waits.
export const PAGE_SIZE = 200

const NO_ROWS: readonly (Person | null)[] = []
const NO_TOTALS = { all: 0, yes: 0, no: 0, unjudged: 0 }
const NO_KINDS: ReadonlyMap<string, number> = new Map()

/** Each loaded row of every slice through `patch` (a write's answer, applied in place). */
export function patchPeople(client: QueryClient, patch: (row: Person) => Person): void {
  client.setQueriesData<PeoplePage>({ queryKey: PEOPLE_QUERY_KEY }, (page) =>
    page ? { ...page, rows: page.rows.map(patch) } : page,
  )
}

/** Reads every slice again: after a write the order, the counts and a filtered tab can change. */
export function refetchPeople(client: QueryClient): Promise<void> {
  return client.invalidateQueries({ queryKey: PEOPLE_QUERY_KEY })
}

/**
 * The view's list a slice at a time (GET /api/people/page): `rows` is as long as the view's match
 * count, with `null` where a slice is not loaded yet; `want` asks for the slices a range of rows
 * needs. A new view starts over at its first slice; the last view's rows stay up until it lands.
 */
export function usePeopleList(view: PeopleView) {
  const query = useMemo(() => viewQuery(view), [view])
  const [wanted, setWanted] = useState<{ query: string; slices: readonly number[] }>({
    query,
    slices: [0],
  })
  const slices = wanted.query === query ? wanted.slices : [0]
  const results = useQueries({
    queries: slices.map((slice) => ({
      queryKey: [...PEOPLE_QUERY_KEY, query, slice],
      queryFn: ({ signal }: { signal: AbortSignal }) =>
        fetchPeoplePage(query, slice * PAGE_SIZE, PAGE_SIZE, signal),
      staleTime: Infinity,
      refetchOnWindowFocus: false,
      retry: false,
    })),
  })
  const updated = results.map((result) => result.dataUpdatedAt).join(",")
  const answered = results[0]?.data

  // The view's rows once its first slice has answered; until then the last view's stay up.
  const current = useMemo(() => {
    if (!answered) return null
    const list = new Array<Person | null>(answered.matching).fill(null)
    results.forEach((result, position) => {
      const start = (slices[position] ?? 0) * PAGE_SIZE
      result.data?.rows.forEach((row, offset) => {
        if (start + offset < list.length) list[start + offset] = row
      })
    })
    return { head: answered, rows: list }
    // A slice's answer changes `updated`; the results array itself is new every render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [query, updated])
  // The last answered view, held across a new view's first request (React's state-from-render pattern).
  const [last, setLast] = useState(current)
  if (current && current !== last) setLast(current)
  const shown = current ?? last
  const head = shown?.head

  const want = useCallback(
    (start: number, end: number) => {
      setWanted((prior) => {
        const held = prior.query === query ? prior.slices : [0]
        const needed: number[] = []
        for (let slice = Math.floor(start / PAGE_SIZE); slice <= Math.floor(end / PAGE_SIZE); slice++)
          if (!held.includes(slice)) needed.push(slice)
        return needed.length || prior.query !== query ? { query, slices: [...held, ...needed] } : prior
      })
    },
    [query],
  )

  const error = shown ? null : (results[0]?.error ?? null)
  return {
    query,
    rows: shown?.rows ?? NO_ROWS,
    matching: head?.matching ?? 0,
    totals: head?.totals ?? NO_TOTALS,
    kinds: head?.kinds ?? NO_KINDS,
    // Nothing to show yet: the loading page.
    loading: !shown && !error,
    error,
    want,
  }
}
