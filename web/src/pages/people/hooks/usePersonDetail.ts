import { useCallback, useEffect, useState } from "react"

import { fetchPersonDetail } from "@/lib/api/people"
import type { PersonDetail } from "@/types/people"

export type DetailState =
  { status: "loading" } | { status: "failed" } | { status: "ready"; detail: PersonDetail }

interface Held {
  id: string
  state: DetailState
}

const LOADING: DetailState = { status: "loading" }

/**
 * The drawer's detail for one person. A new person starts from loading; a refresh
 * (after a write, or Retry) keeps what is shown until the answer lands. The previous
 * request is aborted on every switch, refresh and close.
 */
export function usePersonDetail(id: string | null) {
  const [held, setHeld] = useState<Held | null>(null)
  const [refreshes, setRefreshes] = useState(0)

  useEffect(() => {
    if (id === null) return
    const request = new AbortController()
    fetchPersonDetail(id, request.signal).then(
      (detail) => setHeld({ id, state: { status: "ready", detail } }),
      (error: unknown) => {
        if (error instanceof DOMException && error.name === "AbortError") return
        setHeld({ id, state: { status: "failed" } })
      },
    )
    return () => request.abort()
  }, [id, refreshes])

  const refresh = useCallback(() => setRefreshes((count) => count + 1), [])

  // Another person than `held`: loading until their answer lands. Closed (null): the last
  // person's state stays, so the drawer can animate out with it.
  const state = held !== null && (id === null || held.id === id) ? held.state : LOADING
  return { state, refresh }
}
