import { useEffect, useRef, useState, type ReactNode } from "react"
import { useMutation, useQueryClient } from "@tanstack/react-query"
import { useNavigate } from "react-router-dom"
import { Plus } from "lucide-react"

import { CheckIcon, Spinner } from "@/components/shared"
import type { ConnectionRowProps } from "@/components/shared/ConnectionRow"
import { Button } from "@/components/ui/button"
import { errorText } from "@/lib/api/http"
import { REFRESH_KEY, refreshNow } from "@/lib/api/refresh"
import { readStored, writeStored } from "@/lib/storage"
import { cn } from "@/lib/utils"

// "open" from the first link until Refresh now or Later, then "done": the card shows once.
export const REFRESH_PROMPT_KEY = "accounts.refreshPrompt"
type Stage = "open" | "done"

export interface LinkedSource<Key extends string = string> {
  key: Key
  title: ReactNode
  state: ConnectionRowProps["state"]
}

const stored = () =>
  readStored<Stage>("local", REFRESH_PROMPT_KEY, (raw) => (raw === "open" || raw === "done" ? raw : null))

/** The first time one of `sources` goes from not connected to connected while Accounts is open, a
 *  card offers the refresh that pulls people in (Scheduled tasks' Refresh now) and shows every
 *  source, the ones still to link one click away. It stays, on later visits too, until Refresh now
 *  or Later. A page that loads with sources already connected never opens it: only a row seen as
 *  needed or working that turns ready. */
export function RefreshPrompt<Key extends string>({
  sources,
  onPick,
}: {
  sources: readonly LinkedSource<Key>[]
  onPick: (key: Key) => void
}) {
  const [stage, setStage] = useState(stored)
  const before = useRef<Record<string, LinkedSource["state"]>>({})
  const states = sources.map((source) => `${source.key}:${source.state}`).join(",")
  useEffect(() => {
    const previous = before.current
    before.current = Object.fromEntries(sources.map((source) => [source.key, source.state]))
    if (stage !== null) return
    const linked = sources.some(
      (entry) =>
        entry.state === "ready" && (previous[entry.key] === "needed" || previous[entry.key] === "working"),
    )
    if (!linked) return
    writeStored("local", REFRESH_PROMPT_KEY, "open")
    setStage("open")
    // `states` stands for `sources`, which is a new array on every render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [states])
  const close = () => {
    writeStored("local", REFRESH_PROMPT_KEY, "done")
    setStage("done")
  }
  const navigate = useNavigate()
  const client = useQueryClient()
  const start = useMutation({
    mutationFn: refreshNow,
    onSuccess: (next) => {
      client.setQueryData(REFRESH_KEY, next)
      close()
      void navigate("/tasks")
    },
  })
  if (stage !== "open") return null
  return (
    <section
      aria-label="Refresh after connecting"
      className="flex animate-[fade-in_var(--t-med)_var(--ease-out)] flex-col gap-3 rounded-[var(--radius-m)] border border-line-strong bg-surface-2 p-4"
    >
      <div>
        <p className="m-0 text-sm font-semibold">Bring your people in</p>
        <p className="mb-0 mt-1 text-xs text-muted-foreground">
          Link more sources to find more people, then refresh.
        </p>
      </div>
      <div className="flex flex-wrap gap-2">
        {sources.map((source) => {
          const ready = source.state === "ready"
          return (
            <button
              key={source.key}
              type="button"
              disabled={ready}
              onClick={() => onPick(source.key)}
              className={cn(
                "flex items-center gap-1 rounded-full border px-2 py-0.5 text-[11px]",
                ready
                  ? "cursor-default border-line-strong bg-transparent text-foreground"
                  : "cursor-pointer border-dashed border-line-strong bg-transparent text-muted-foreground hover:border-solid hover:text-foreground",
              )}
            >
              {ready ? <CheckIcon className="size-3 text-ok" /> : <Plus aria-hidden className="size-3" />}
              {source.title}
            </button>
          )
        })}
      </div>
      <div className="flex items-center gap-2">
        <Button variant="primary" size="lg" disabled={start.isPending} onClick={() => start.mutate()}>
          {start.isPending ? <Spinner className="size-3.5" /> : null}
          Refresh now
        </Button>
        <Button variant="ghost" size="lg" onClick={close}>
          Later
        </Button>
      </div>
      {start.error ? (
        <p role="alert" className="m-0 text-xs text-bad">
          {errorText(start.error)}
        </p>
      ) : null}
    </section>
  )
}
