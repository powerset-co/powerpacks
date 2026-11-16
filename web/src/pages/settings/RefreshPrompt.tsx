import type { ReactNode } from "react"
import { useMutation, useQueryClient } from "@tanstack/react-query"
import { useNavigate } from "react-router-dom"
import { Plus } from "lucide-react"

import { CheckIcon, Spinner } from "@/components/shared"
import type { ConnectionRowProps } from "@/components/shared/ConnectionRow"
import { Button } from "@/components/ui/button"
import { errorText } from "@/lib/api/http"
import { REFRESH_KEY, refreshNow } from "@/lib/api/refresh"
import { cn } from "@/lib/utils"
import { useRefresh } from "@/pages/tasks/useRefresh"

export interface PromptSource<Key extends string = string> {
  key: Key
  title: ReactNode
  state: ConnectionRowProps["state"]
  // Its first import has run (the account's last sync), so its people are in.
  imported: boolean
}

/** While a connected source has never been imported, a card offers the refresh that brings its
 *  people in (Scheduled tasks' Refresh now) and shows every source, the ones still to link one
 *  click away. It has no dismiss: it goes once the refresh has imported every connected source. */
export function RefreshPrompt<Key extends string>({
  sources,
  onPick,
}: {
  sources: readonly PromptSource<Key>[]
  onPick: (key: Key) => void
}) {
  const navigate = useNavigate()
  const client = useQueryClient()
  const refresh = useRefresh()
  const start = useMutation({
    mutationFn: refreshNow,
    onSuccess: (next) => {
      client.setQueryData(REFRESH_KEY, next)
      void navigate("/tasks")
    },
  })
  if (!sources.some((source) => source.state === "ready" && !source.imported)) return null
  const running = Boolean(refresh.data?.running)
  return (
    <section
      aria-label="Refresh after connecting"
      className="flex animate-[fade-in_var(--t-med)_var(--ease-out)] flex-col gap-3 rounded-[var(--radius-m)] border border-line-strong bg-surface-2 p-4"
    >
      <div>
        <p className="m-0 text-sm font-semibold">Grow your network</p>
        <p className="mb-0 mt-1 text-xs text-muted-foreground">
          Link sources to find more people, then refresh.
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
      <Button
        variant="primary"
        size="lg"
        className="self-start"
        disabled={running || start.isPending}
        onClick={() => start.mutate()}
      >
        {running || start.isPending ? <Spinner className="size-3.5" /> : null}
        {running ? "Refreshing…" : "Refresh now"}
      </Button>
      {start.error ? (
        <p role="alert" className="m-0 text-xs text-bad">
          {errorText(start.error)}
        </p>
      ) : null}
    </section>
  )
}
