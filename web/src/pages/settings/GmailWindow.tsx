import { useQuery, useQueryClient } from "@tanstack/react-query"
import { useState } from "react"

import { Spinner } from "@/components/shared"
import { Button } from "@/components/ui/button"
import { fetchGmailWindows, syncGmailWindow } from "@/lib/api/accounts"
import { errorText } from "@/lib/api/http"

const STOPS = [
  { years: 1, label: "1 year" },
  { years: 3, label: "3 years" },
  { years: 5, label: "5 years" },
  { years: 0, label: "Everything" },
] as const

function time(seconds: number): string {
  if (seconds < 60) return "under a minute"
  if (seconds < 3600) return `about ${Math.round(seconds / 60)} min`
  return `about ${Math.floor(seconds / 3600)} h ${Math.round((seconds % 3600) / 60)} min`
}

/** How far back one mailbox's sync goes: a slider over four windows, each with what it adds
 *  and how long that takes at the pace this mailbox's syncs have run. The counts come from
 *  Gmail, with the sync's own query, when the card opens. */
export function GmailWindow({ email }: { email: string }) {
  const client = useQueryClient()
  const [at, setAt] = useState(0)
  const [error, setError] = useState<string | null>(null)
  const [starting, setStarting] = useState(false)
  const counts = useQuery({
    queryKey: ["accounts", "gmail-windows", email],
    queryFn: ({ signal }) => fetchGmailWindows(email, signal),
    staleTime: 5 * 60_000,
    retry: false,
  })
  const stop = STOPS[at] ?? STOPS[0]
  const total = counts.data?.windows.find((window) => window.years === stop.years)?.messages ?? null
  const have = counts.data ? Math.min(counts.data.synced, total ?? 0) : 0
  const more = total === null ? null : Math.max(0, total - have)
  const pace = counts.data?.pace ?? 2
  const start = () => {
    setStarting(true)
    setError(null)
    syncGmailWindow(email, stop.years)
      .then(() => client.invalidateQueries({ queryKey: ["accounts"] }))
      .catch((caught: unknown) => setError(errorText(caught)))
      .finally(() => setStarting(false))
  }
  return (
    <div className="mt-3 flex flex-col gap-3 border-t border-line pt-3" data-gmail-window>
      <div className="flex items-center justify-between gap-3">
        <span className="text-[13px] font-semibold">How far back should Gmail go?</span>
        {counts.isPending ? (
          <span className="flex items-center gap-2 text-xs text-muted-foreground">
            <Spinner /> Counting your mail…
          </span>
        ) : null}
      </div>
      <input
        type="range"
        min={0}
        max={STOPS.length - 1}
        step={1}
        value={at}
        aria-label="How far back"
        onChange={(event) => setAt(Number(event.target.value))}
        className="w-full accent-[var(--primary)]"
      />
      <div className="flex justify-between px-0.5 text-[11px] uppercase tracking-[.08em] text-faint">
        {STOPS.map((option, index) => (
          <span
            key={option.years}
            aria-current={index === at ? "true" : undefined}
            className="aria-[current=true]:font-semibold aria-[current=true]:text-foreground"
          >
            {option.label}
          </span>
        ))}
      </div>
      {counts.error ? (
        <p className="m-0 text-xs text-bad">{errorText(counts.error)}</p>
      ) : counts.data && total !== null && more !== null ? (
        <div className="flex flex-col gap-1 text-xs text-muted-foreground">
          <div className="h-2 overflow-hidden rounded-full bg-[var(--track,var(--secondary))]" aria-hidden>
            <div className="h-full bg-ok" style={{ width: `${total ? (have / total) * 100 : 0}%` }} />
          </div>
          <span>
            {total.toLocaleString()} messages in {stop.label.toLowerCase()} · {have.toLocaleString()} already
            synced
            {more ? ` · ${more.toLocaleString()} more, ${time(more / pace)}` : " · nothing to do"}
          </span>
        </div>
      ) : null}
      <div className="flex items-center gap-3">
        <Button
          size="sm"
          variant="primary"
          disabled={starting || counts.isPending || more === 0}
          onClick={start}
        >
          {starting ? (
            <>
              <Spinner /> Starting…
            </>
          ) : (
            `Sync ${stop.label.toLowerCase()}`
          )}
        </Button>
        {error ? <span className="text-xs text-bad">{error}</span> : null}
      </div>
    </div>
  )
}
