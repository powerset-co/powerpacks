import { useMutation, useQueryClient } from "@tanstack/react-query"

import { Button } from "@/components/ui/button"
import { ago } from "@/lib/copy"
import { errorText } from "@/lib/api/http"
import { MEMORY_KEY, setKeepUpdated, updateMemory } from "@/lib/api/memory"
import type { MemoryJob } from "@/types/memory"

/** Memory is on: its state, Update now, the keep-updated switch, and the directory's search. */
export function Enabled({ record }: { record: MemoryJob }) {
  const client = useQueryClient()
  const update = useMutation({
    mutationFn: updateMemory,
    onSuccess: (next) => client.setQueryData(MEMORY_KEY, next),
  })
  const settings = useMutation({
    mutationFn: setKeepUpdated,
    onSuccess: (next) => client.setQueryData(MEMORY_KEY, next),
  })
  const state =
    record.status === "failed"
      ? "Last update failed"
      : record.stopped
        ? `Ready · stopped early: ${record.stopped}`
        : "Ready"
  const error = update.error ?? settings.error
  return (
    <>
      <section className="flex flex-col gap-3" aria-labelledby="memory-state">
        <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
          <div className="mr-auto flex min-w-0 flex-col gap-1">
            <h2 id="memory-state" className="m-0 text-[15px] font-semibold">
              {state}
            </h2>
            <p className="m-0 text-sm text-muted-foreground tabular-nums">
              Updated {ago(record.updated_at || null)} · {record.people_indexed.toLocaleString()} people
              indexed
            </p>
          </div>
          <Button type="button" onClick={() => update.mutate()} disabled={update.isPending}>
            {update.isPending ? "Starting…" : "Update now"}
          </Button>
        </div>
        {record.status === "failed" && record.error ? (
          <p role="alert" className="m-0 text-xs text-bad [overflow-wrap:anywhere]">
            {record.error}
          </p>
        ) : null}
        <label className="flex w-fit cursor-pointer items-center gap-3 text-[13px]">
          <input
            type="checkbox"
            checked={record.keep_updated}
            disabled={settings.isPending}
            onChange={() => settings.mutate(!record.keep_updated)}
            className="accent-[var(--primary)]"
          />
          Keep memory updated
        </label>
        <p className="m-0 -mt-1 text-xs text-muted-foreground">
          {record.keep_updated
            ? "Memory updates after Deep Context finishes and with the daily refresh in Scheduled tasks."
            : "Memory stays searchable; it updates only when you click Update now."}
        </p>
        {error ? (
          <p role="alert" className="m-0 text-xs text-bad">
            {errorText(error)}
          </p>
        ) : null}
      </section>
    </>
  )
}
