import { useMutation, useQueryClient } from "@tanstack/react-query"
import { useId, useState } from "react"

import { Button } from "@/components/ui/button"
import { errorText } from "@/lib/api/http"
import { enableMemory, MEMORY_KEY } from "@/lib/api/memory"
import type { MemoryJob } from "@/types/memory"

import { dollars, tokenText } from "./format"

/** Before Memory is on: what it reads, what it costs, and the spend the user approves. */
export function FirstRun({ record }: { record: MemoryJob }) {
  const client = useQueryClient()
  const limitId = useId()
  const estimate = record.estimate ?? null
  const cost = estimate?.estimated_cost_usd ?? null
  // Rounded up to the cent, so the prefilled limit covers the estimate.
  const [limit, setLimit] = useState(cost === null ? "" : (Math.ceil(cost * 100) / 100).toFixed(2))
  const enable = useMutation({
    mutationFn: (amount: number) => enableMemory(amount),
    onSuccess: (next) => client.setQueryData(MEMORY_KEY, next),
  })
  const amount = Number(limit)
  const valid = limit.trim() !== "" && Number.isFinite(amount) && amount >= 0
  const people = record.inventory?.counts.parents ?? 0
  const error = enable.error ? errorText(enable.error) : record.status === "failed" ? record.error : ""
  return (
    <section className="flex flex-col gap-5" aria-labelledby="memory-first-run">
      <div className="flex flex-col gap-1.5">
        <h2 id="memory-first-run" className="m-0 text-[15px] font-semibold">
          Remember what you know about people
        </h2>
        <p className="m-0 max-w-[620px] text-sm text-muted-foreground">
          Memory reads what Deep Context saved about each person and turns it into a directory you and your
          agent can search: who you tagged as an investor, who mentioned a topic, who works where. It runs on
          this computer; only the text it reads goes to OpenAI.
        </p>
      </div>
      {estimate === null ? (
        <p className="m-0 text-sm text-muted-foreground">
          Nothing to remember yet: process your contacts with Deep Context first, then come back.
        </p>
      ) : (
        <>
          <dl className="m-0 grid grid-cols-[max-content_minmax(0,1fr)] gap-x-6 gap-y-2 text-[13px] max-[520px]:grid-cols-1 max-[520px]:gap-y-0.5">
            <dt className="text-muted-foreground">People</dt>
            <dd className="m-0 tabular-nums max-[520px]:mb-2">{people.toLocaleString()}</dd>
            <dt className="text-muted-foreground">Sources included</dt>
            <dd className="m-0 max-[520px]:mb-2">{record.sources?.join(", ")}</dd>
            <dt className="text-muted-foreground">Estimated cost</dt>
            <dd className="m-0 tabular-nums">
              {cost === null
                ? `${tokenText(estimate.generation_input_tokens + estimate.generation_output_token_cap + estimate.embedding_input_tokens)} (prices unknown)`
                : `about ${dollars(cost)}`}
            </dd>
          </dl>
          <form
            className="flex flex-wrap items-end gap-3"
            onSubmit={(event) => {
              event.preventDefault()
              if (valid) enable.mutate(amount)
            }}
          >
            <div className="flex flex-col gap-1">
              <label htmlFor={limitId} className="text-xs text-muted-foreground">
                Spend limit (USD)
              </label>
              <span className="flex h-8 items-center rounded-sm border border-border bg-card pl-2.5 focus-within:border-primary">
                <span aria-hidden="true" className="text-[13px] text-faint">
                  $
                </span>
                <input
                  id={limitId}
                  type="number"
                  inputMode="decimal"
                  min={0}
                  step={0.01}
                  value={limit}
                  onChange={(event) => setLimit(event.target.value)}
                  className="h-full w-28 border-0 bg-transparent px-1.5 text-[13px] text-foreground tabular-nums outline-none"
                />
              </span>
            </div>
            <Button type="submit" variant="primary" disabled={!valid || enable.isPending}>
              {enable.isPending ? "Enabling…" : "Enable Memory"}
            </Button>
          </form>
          <p className="m-0 -mt-2 text-xs text-muted-foreground">
            The run stops when it reaches the limit; what it finished stays searchable.
          </p>
        </>
      )}
      {error ? (
        <p role="alert" className="m-0 text-xs text-bad">
          {error}
        </p>
      ) : null}
    </section>
  )
}
