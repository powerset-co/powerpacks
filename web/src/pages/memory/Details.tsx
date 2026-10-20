import { useState } from "react"

import { DetailsSection } from "@/components/shared"
import { stamp } from "@/lib/copy"
import type { MemoryJob } from "@/types/memory"

import { spentText, stepLabel, STEPS } from "./format"

/** The run's coverage per step, what failed (the next update retries it first), and its cost. */
export function Details({ record }: { record: MemoryJob }) {
  const [open, setOpen] = useState(false)
  return (
    <DetailsSection sectionKey="memory-details" title="Details" open={open} onToggle={setOpen}>
      <div className="flex flex-col gap-5 text-[13px]">
        <div className="flex flex-col gap-2">
          <h4 className="m-0 text-xs font-semibold text-muted-foreground">Source coverage</h4>
          <dl className="m-0 grid grid-cols-[minmax(0,1fr)_auto] gap-x-6 gap-y-1.5">
            {STEPS.map((step) => (
              <div key={step.key} className="contents">
                <dt>{step.label}</dt>
                <dd className="m-0 text-right tabular-nums">
                  {record.steps[step.key].done.toLocaleString()}
                  {record.steps[step.key].total
                    ? ` / ${record.steps[step.key].total.toLocaleString()}`
                    : ""}{" "}
                  {step.unit}
                </dd>
              </div>
            ))}
            <dt>People indexed</dt>
            <dd className="m-0 text-right tabular-nums">{record.people_indexed.toLocaleString()}</dd>
          </dl>
        </div>
        <div className="flex flex-col gap-2">
          <h4 className="m-0 text-xs font-semibold text-muted-foreground">
            Failures {record.failures.length ? `(${record.failures.length.toLocaleString()})` : ""}
          </h4>
          {record.failures.length ? (
            <>
              <p className="m-0 text-xs text-muted-foreground">Update now retries these first.</p>
              <ul className="m-0 flex list-none flex-col gap-2 p-0">
                {record.failures.map((failure) => (
                  <li key={`${failure.step}:${failure.key}`} className="flex flex-col gap-0.5">
                    <span className="break-all">
                      <span className="text-muted-foreground">{stepLabel(failure.step)}</span> · {failure.key}
                    </span>
                    <span className="text-xs text-bad [overflow-wrap:anywhere]">{failure.error}</span>
                    <span className="text-[11px] text-faint">{stamp(failure.at)}</span>
                  </li>
                ))}
              </ul>
            </>
          ) : (
            <p className="m-0 text-muted-foreground">None.</p>
          )}
        </div>
        <div className="flex flex-col gap-2">
          <h4 className="m-0 text-xs font-semibold text-muted-foreground">Cost of the last run</h4>
          <p className="m-0 tabular-nums">
            {spentText(record)}
            {record.spent_usd !== null ? "" : " (prices unknown)"}
          </p>
          <p className="m-0 text-xs text-muted-foreground tabular-nums">
            {record.tokens.input.toLocaleString()} input, {record.tokens.output.toLocaleString()} output,{" "}
            {record.tokens.embedding.toLocaleString()} embedding tokens
          </p>
        </div>
      </div>
    </DetailsSection>
  )
}
