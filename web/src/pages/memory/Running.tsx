import { useMutation, useQueryClient } from "@tanstack/react-query"

import { ProgressBar } from "@/components/shared"
import { Button } from "@/components/ui/button"
import { errorText } from "@/lib/api/http"
import { MEMORY_KEY, stopMemory } from "@/lib/api/memory"
import { cn } from "@/lib/utils"
import type { MemoryJob } from "@/types/memory"

import { spentText, STEPS } from "./format"

/** A run at work: the four steps, the current one's counts and item, and what it has spent. */
export function Running({ record }: { record: MemoryJob }) {
  const client = useQueryClient()
  const stop = useMutation({
    mutationFn: stopMemory,
    onSuccess: (next) => client.setQueryData(MEMORY_KEY, next),
  })
  const current = STEPS.findIndex(({ key }) => key === record.step)
  return (
    <section className="flex flex-col gap-4" aria-labelledby="memory-running" aria-live="polite">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
        <div className="mr-auto flex min-w-0 flex-col gap-1">
          <h2 id="memory-running" className="m-0 text-[15px] font-semibold">
            Building your memory
          </h2>
          <p className="m-0 text-sm text-muted-foreground">
            Closing the page keeps it running. Spent so far: {spentText(record)}.
          </p>
        </div>
        <Button type="button" onClick={() => stop.mutate()} disabled={stop.isPending}>
          {stop.isPending ? "Stopping…" : "Stop"}
        </Button>
      </div>
      {stop.error ? (
        <p role="alert" className="m-0 text-xs text-bad">
          {errorText(stop.error)}
        </p>
      ) : null}
      <ol
        className="m-0 flex max-w-[620px] list-none flex-col gap-4 p-0 text-[13px]"
        aria-label="Memory steps"
      >
        {STEPS.map((step, index) => {
          const counts = record.steps[step.key]
          const done = index < current
          const active = index === current
          return (
            <li
              key={step.key}
              aria-current={active ? "step" : undefined}
              className={cn("flex flex-col gap-1.5", active ? "text-foreground" : "text-muted-foreground")}
            >
              <div className="grid grid-cols-[20px_minmax(0,1fr)_auto] items-center gap-3">
                <span aria-hidden="true" className={cn("text-center", done && "text-ok")}>
                  {done ? "✓" : active ? "•" : "○"}
                </span>
                <span className={cn(active && "font-semibold")}>{step.label}</span>
                <span className="text-xs tabular-nums">
                  {!done && !active
                    ? "Next"
                    : counts.total
                      ? `${counts.done.toLocaleString()} / ${counts.total.toLocaleString()} ${step.unit}`
                      : counts.done || done
                        ? `${counts.done.toLocaleString()} ${step.unit} completed`
                        : "Starting"}
                </span>
              </div>
              {active ? (
                <div className="flex flex-col gap-1.5 pl-8">
                  <ProgressBar label={step.label} done={counts.done} total={counts.total} />
                </div>
              ) : null}
            </li>
          )
        })}
      </ol>
    </section>
  )
}
