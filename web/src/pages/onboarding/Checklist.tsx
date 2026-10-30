// The checklist Set up and Import share: the golden rows the run plans (setup-steps-mapping.json),
// the running row's line and time left, and a stop's way out.

import { CheckIcon, Spinner } from "@/components/shared"
import { Button } from "@/components/ui/button"
import { errorText } from "@/lib/api/http"
import { cn } from "@/lib/utils"
import type { InstallStatus } from "@/types/install"

import MAPPING from "../../../../packs/powerset/primitives/install/setup-steps-mapping.json"

import { Note } from "./Frame"
import { stopDetail } from "@/pages/install/sources"

import { leftText } from "./pace"
import type { Run } from "./run"

const DONE = new Set(["completed", "skipped"])

type Row = (typeof MAPPING.rows)[number]
type Standing = "done" | "current" | "stopped" | "next"

/** The row's steps this run plans. */
function planned(data: InstallStatus, row: Row): string[] {
  return row.steps.filter((step) => data.plan?.includes(step))
}

/** Where a row stands: done once every planned step is completed or skipped, current while the
 *  run is on one of its steps (stopped if that step failed), next otherwise. */
function standingOf(data: InstallStatus, row: Row): Standing {
  if (planned(data, row).every((step) => DONE.has(data.steps[step]?.status ?? ""))) return "done"
  if (row.steps.includes(data.step)) return data.status === "failed" ? "stopped" : "current"
  return "next"
}

/** A finished row says the record's line for its last planned step. */
function finishedLine(data: InstallStatus, row: Row): string {
  const last = planned(data, row).at(-1)
  return last ? (data.steps[last]?.message ?? row.label) : row.label
}

/** A stop's way out: the record's note, Try again, the detail behind the stop, where the logs are. */
function Stopped({ data, retrying, retry }: { data: InstallStatus; retrying: boolean; retry: () => void }) {
  return (
    <div className="flex flex-col gap-2">
      {data.note ? <Note>{data.note}</Note> : null}
      <Button variant="primary" className="self-start" disabled={retrying} onClick={retry}>
        {retrying ? (
          <>
            <Spinner /> Starting…
          </>
        ) : (
          "Try again"
        )}
      </Button>
      {stopDetail(data) ? <Note bad>{stopDetail(data)}</Note> : null}
      <Note>Logs are in {data.log_path.replace(/\/[^/]*$/, "/")}</Note>
    </div>
  )
}

/** The heading and the rows the run plans; while there is no record of this screen's run yet,
 *  a spinner line. */
export function Checklist({ run }: { run: Run }) {
  const { data, failed, ready, left, retrying, retry } = run
  // The rows this run plans; a row with `needs` only when that step is planned.
  const rows = data
    ? MAPPING.rows.filter(
        (row) =>
          planned(data, row).length > 0 && (row.needs === "" || data.plan?.includes(row.needs) === true),
      )
    : []
  const current = data ? rows.find((row) => row.steps.includes(data.step)) : undefined
  return (
    <>
      <h1 className="m-0 text-[22px] font-bold leading-tight">
        {failed ? "Setup stopped" : ready ? "Search is ready" : (current?.label ?? "Setting up")}
      </h1>
      {run.error ? <Note bad>{errorText(run.error)}</Note> : null}
      {data && rows.length > 0 ? (
        <ol className="m-0 flex list-none flex-col gap-3 p-0" aria-label="Setup stages">
          {rows.map((row) => {
            const standing = standingOf(data, row)
            const live = standing === "current" || standing === "stopped"
            return (
              <li key={row.label} className="flex flex-col gap-2" data-stage={standing}>
                <div
                  className={cn(
                    "flex items-center gap-3 text-[13px] transition-colors duration-med",
                    standing === "next" ? "text-faint" : "text-foreground",
                  )}
                >
                  <span className="grid size-5 shrink-0 place-items-center" aria-hidden>
                    {standing === "done" ? (
                      <CheckIcon className="size-4 text-ok" />
                    ) : standing === "current" ? (
                      <Spinner className="size-3.5 text-primary" />
                    ) : (
                      <span
                        className={cn(
                          "size-1.5 rounded-full",
                          standing === "stopped" ? "bg-bad" : "bg-line-strong",
                        )}
                      />
                    )}
                  </span>
                  <span className={cn(live && "font-semibold")}>
                    {standing === "done" ? finishedLine(data, row) : row.label}
                  </span>
                </div>
                {live ? (
                  <div className="flex flex-col gap-2 pl-8">
                    <div className="flex justify-between gap-3 text-xs">
                      <span className={standing === "stopped" ? "text-bad" : "text-muted-foreground"}>
                        {data.message}
                      </span>
                      <span className="shrink-0 text-muted-foreground">
                        {left !== null ? leftText(left) : ""}
                      </span>
                    </div>
                    {standing === "stopped" ? (
                      <Stopped data={data} retrying={retrying} retry={retry} />
                    ) : null}
                  </div>
                ) : null}
              </li>
            )
          })}
        </ol>
      ) : null}
      {data && failed && current === undefined ? (
        // Stopped on a step no row lists: the stop itself, with its way out.
        <div className="flex flex-col gap-2">
          <span className="text-xs text-bad">{data.message}</span>
          <Stopped data={data} retrying={retrying} retry={retry} />
        </div>
      ) : null}
      {!data || (rows.length === 0 && !failed) ? (
        <Note>
          <span className="inline-flex items-center gap-2">
            <Spinner /> {data ? data.message : run.starting ? "Starting…" : "Reading your progress…"}
          </span>
        </Note>
      ) : null}
      {run.retryError ? <Note bad>{run.retryError}</Note> : null}
    </>
  )
}
