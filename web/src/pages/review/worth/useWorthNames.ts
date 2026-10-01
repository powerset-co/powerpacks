import { useCallback, useEffect, useMemo, useState } from "react"

import { errorText } from "@/lib/api/http"
import { fetchWorthPending } from "@/lib/api/review"
import { EMPTY, toggled } from "@/lib/sets"
import type { WorthPendingEntry } from "@/types/review"

import { useReview } from "../hooks/useReview"

export interface WorthNames {
  /** The screen opened with people pending, so it has a search box (which then stays). */
  searchable: boolean
  /** The people still pending, in queue order. */
  pending: readonly WorthPendingEntry[]
  /** The person is decided: their name leaves the search. */
  forget: (key: string) => void
}

/** The typeahead's names: read once when the review tab opens, then pruned here as decisions
 *  settle. */
export function useWorthNames(): WorthNames {
  const { toastError } = useReview()
  const [read, setRead] = useState<readonly WorthPendingEntry[]>([])
  const [decided, setDecided] = useState(EMPTY)

  useEffect(() => {
    const gone = new AbortController()
    fetchWorthPending(gone.signal).then(setRead, (error: unknown) => {
      if (!gone.signal.aborted) toastError(errorText(error))
    })
    return () => gone.abort()
  }, [toastError])

  const forget = useCallback(
    (key: string) => setDecided((keys) => toggled(keys, key.toLowerCase(), true)),
    [],
  )
  const pending = useMemo(
    () => read.filter((entry) => !decided.has(entry.key.toLowerCase())),
    [read, decided],
  )
  return { searchable: read.length > 0, pending, forget }
}
