import { useEffect, useRef, useState, type ReactNode } from "react"
import { useMutation, useQueryClient } from "@tanstack/react-query"
import { useNavigate } from "react-router-dom"

import { Spinner } from "@/components/shared"
import type { ConnectionRowProps } from "@/components/shared/ConnectionRow"
import { Button } from "@/components/ui/button"
import { errorText } from "@/lib/api/http"
import { REFRESH_KEY, refreshNow } from "@/lib/api/refresh"
import { readStored, writeStored } from "@/lib/storage"

// The sources the prompt has shown for on this Mac, so each shows it once, ever.
export const REFRESH_PROMPT_KEY = "accounts.refreshPrompted"

export interface LinkedSource {
  key: string
  title: ReactNode
  icon: ReactNode
  state: ConnectionRowProps["state"]
}

const prompted = () =>
  readStored("local", REFRESH_PROMPT_KEY, (raw) =>
    Array.isArray(raw) ? raw.filter((key): key is string => typeof key === "string") : null,
  ) ?? []

/** When one of `sources` goes from not connected to connected while Accounts is open, a card
 *  offers the refresh that pulls its people in (Scheduled tasks' Refresh now), once per source.
 *  Loading a page whose sources are already connected never counts: only a row seen as needed or
 *  working that turns ready. */
export function RefreshPrompt({ sources }: { sources: readonly LinkedSource[] }) {
  const [linked, setLinked] = useState<LinkedSource | null>(null)
  const before = useRef<Record<string, LinkedSource["state"]>>({})
  const states = sources.map((source) => `${source.key}:${source.state}`).join(",")
  useEffect(() => {
    const previous = before.current
    before.current = Object.fromEntries(sources.map((source) => [source.key, source.state]))
    const done = prompted()
    const source = sources.find(
      (entry) =>
        !done.includes(entry.key) &&
        entry.state === "ready" &&
        (previous[entry.key] === "needed" || previous[entry.key] === "working"),
    )
    if (!source) return
    writeStored("local", REFRESH_PROMPT_KEY, [...done, source.key])
    setLinked(source)
    // `states` stands for `sources`, which is a new array on every render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [states])
  const navigate = useNavigate()
  const client = useQueryClient()
  const start = useMutation({
    mutationFn: refreshNow,
    onSuccess: (next) => {
      client.setQueryData(REFRESH_KEY, next)
      void navigate("/tasks")
    },
  })
  if (!linked) return null
  return (
    <section
      aria-label="Refresh after connecting"
      className="flex animate-[fade-in_var(--t-med)_var(--ease-out)] items-start gap-3 rounded-[var(--radius-m)] border border-line-strong bg-surface-2 p-4"
    >
      <span className="grid size-9 shrink-0 place-items-center rounded-[var(--radius-s)] bg-card">
        {linked.icon}
      </span>
      <div className="min-w-0 flex-1">
        <p className="m-0 text-sm font-semibold">{linked.title} connected</p>
        <p className="mb-0 mt-1 text-xs text-muted-foreground">Refresh now to bring your people in.</p>
        <div className="mt-3 flex items-center gap-2">
          <Button variant="primary" size="lg" disabled={start.isPending} onClick={() => start.mutate()}>
            {start.isPending ? <Spinner className="size-3.5" /> : null}
            Refresh now
          </Button>
          <Button variant="ghost" size="lg" onClick={() => setLinked(null)}>
            Later
          </Button>
        </div>
        {start.error ? (
          <p role="alert" className="mb-0 mt-2 text-xs text-bad">
            {errorText(start.error)}
          </p>
        ) : null}
      </div>
    </section>
  )
}
