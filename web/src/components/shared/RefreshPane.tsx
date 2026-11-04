import { useState } from "react"
import { useNavigate } from "react-router-dom"
import { TriangleAlert } from "lucide-react"

import { readStored, writeStored } from "@/lib/storage"
import { useRefresh } from "@/pages/tasks/useRefresh"

import { Button } from "../ui/button"
import { CLOSE_MARK } from "./icons/actions"
import { Tip } from "./Tip"

// The last refresh run whose pane was dismissed; a later run with problems shows again.
const DISMISSED_KEY = "refresh-pane-dismissed"

/** What the last daily refresh needs from the person: its first problem, worded as what to do,
 *  with a button that goes where it is done, and how many more wait on Scheduled tasks. */
export function RefreshPane({ rail }: { rail: boolean }) {
  const navigate = useNavigate()
  const refresh = useRefresh()
  const [dismissed, setDismissed] = useState(() =>
    readStored("local", DISMISSED_KEY, (raw) => (typeof raw === "number" ? raw : null)),
  )
  const last = refresh.data?.runs.find((run) => run.status !== "running")
  const problem = last?.problems[0]
  if (!last || !problem || dismissed === last.id) return null
  const more = last.problems.length - 1
  const dismiss = () => {
    writeStored("local", DISMISSED_KEY, last.id)
    setDismissed(last.id)
  }
  if (rail) {
    return (
      <div className="flex justify-center pb-1">
        <button
          type="button"
          aria-label={`${problem.title}. ${problem.action}`}
          onClick={() => void navigate(problem.href)}
          className="group relative grid size-9 cursor-pointer place-items-center rounded-[var(--radius-s)] border-0 bg-transparent p-0 text-bad transition-colors duration-fast ease-out hover:bg-secondary"
        >
          <TriangleAlert className="size-[18px]" />
          <Tip side="right">{problem.title}</Tip>
        </button>
      </div>
    )
  }
  return (
    <div
      role="alert"
      data-refresh-problem={problem.source}
      className="mx-2 mb-2 rounded-[var(--radius-m)] border border-[color-mix(in_srgb,var(--bad)_45%,var(--line))] bg-[color-mix(in_srgb,var(--surface-2)_70%,transparent)] p-2.5"
    >
      <div className="flex items-start gap-2">
        <TriangleAlert className="mt-px size-4 shrink-0 text-bad" />
        <span className="min-w-0 flex-1 leading-[1.25]">
          <span className="block text-[12px] font-semibold text-foreground">{problem.title}</span>
          <span className="mt-0.5 block text-[11px] text-muted-foreground">{problem.detail}</span>
        </span>
        <button
          type="button"
          aria-label="Dismiss"
          onClick={dismiss}
          className="-mt-1 -mr-1 grid size-6 cursor-pointer place-items-center rounded-[var(--radius-s)] border-0 bg-transparent p-0 text-[15px] leading-none text-faint transition-colors duration-fast ease-out hover:bg-secondary hover:text-foreground"
        >
          {CLOSE_MARK}
        </button>
      </div>
      <Button
        variant="primary"
        size="sm"
        onClick={() => void navigate(problem.href)}
        className="mt-2.5 w-full"
      >
        {problem.action}
      </Button>
      {more > 0 && (
        <button
          type="button"
          onClick={() => void navigate("/tasks")}
          className="mt-1.5 cursor-pointer border-0 bg-transparent p-0 text-left text-[11px] text-muted-foreground hover:text-foreground"
        >
          +{more} more: {last.problems[1]?.title}
        </button>
      )}
    </div>
  )
}
