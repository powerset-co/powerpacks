import { useMutation, useQueryClient } from "@tanstack/react-query"

import { Button } from "@/components/ui/button"
import { ago } from "@/lib/copy"
import { errorText } from "@/lib/api/http"
import { MEMORY_KEY, updateMemory } from "@/lib/api/memory"
import type { MemoryJob } from "@/types/memory"

/** Compact status above the graph; automatic refresh stays enabled by default. */
export function Enabled({ record }: { record: MemoryJob }) {
  const client = useQueryClient()
  const update = useMutation({
    mutationFn: updateMemory,
    onSuccess: (next) => client.setQueryData(MEMORY_KEY, next),
  })
  const state =
    record.status === "failed"
      ? "Last update failed"
      : record.stopped
        ? `Ready · stopped early: ${record.stopped}`
        : "Ready"
  const error = update.error
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
          <Button
            type="button"
            onClick={() => update.mutate()}
            disabled={update.isPending || record.update_available === false}
          >
            {update.isPending ? "Starting…" : record.update_available === false ? "Updated" : "Update now"}
          </Button>
        </div>
        {record.status === "failed" && record.error ? (
          <p role="alert" className="m-0 text-xs text-bad [overflow-wrap:anywhere]">
            {record.error}
          </p>
        ) : null}
        {error ? (
          <p role="alert" className="m-0 text-xs text-bad">
            {errorText(error)}
          </p>
        ) : null}
      </section>
    </>
  )
}
