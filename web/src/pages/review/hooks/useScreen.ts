import { useQuery } from "@tanstack/react-query"
import { useCallback, useState } from "react"
import { useLocation, useNavigate } from "react-router-dom"

import { fetchReviewPage } from "@/lib/api/review"
import { readScreenQuery } from "@/lib/review/links"
import type { ReviewPage } from "@/types/review"

/** One load of the page: what the server answered and how the URL asked for it. */
export interface Screen {
  /** Counts the loads; a new one is a new screen (the stage remounts, per-screen state resets). */
  id: number
  page: ReviewPage
  preview: boolean
  debug: boolean
  index: number
}

/**
 * The screen for the address bar. Every navigation is a fresh read, as a document load would
 * be: a link to the URL already open reads again, and so do back and forward (the query is
 * keyed by the history entry and never cached). The screen on show stays until the next
 * one's answer lands.
 */
export function useScreen() {
  const location = useLocation()
  const navigate = useNavigate()
  const asked = readScreenQuery(location.search)
  const query = useQuery({
    queryKey: ["review", "page", location.key, asked.stage, asked.view],
    queryFn: ({ signal }) => fetchReviewPage(asked.stage, asked.view, signal),
    // A reload must yield a new object even when nothing changed: the stage remounts on it.
    structuralSharing: false,
    gcTime: 0,
    staleTime: Infinity,
    retry: false,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
  })

  const [screen, setScreen] = useState<Screen | null>(null)
  if (query.data && query.data !== screen?.page) {
    setScreen({
      id: (screen?.id ?? 0) + 1,
      page: query.data,
      preview: asked.preview,
      debug: asked.debug,
      index: asked.index,
    })
  }

  const { refetch } = query
  const reload = useCallback(() => void refetch(), [refetch])
  // Opening the URL already open replaces its history entry, as a document load of it would.
  const here = location.pathname + location.search
  const open = useCallback(
    (href: string) => void navigate(href, { replace: href === here }),
    [navigate, here],
  )

  return { screen, error: query.error, reload, open }
}
