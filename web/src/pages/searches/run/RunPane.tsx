import { useRef, useState, type ReactNode } from "react"

import { EmptyState } from "@/components/shared"
import type { SearchRunPayload } from "@/types/searches"

import { useRunSwap } from "../hooks/useRunSwap"
import { useSearchRun } from "../hooks/useSearchRun"
import { RunEmpty } from "./RunEmpty"
import { RunLoading } from "./RunLoading"

interface RunPaneProps {
  runId: string | null
  // What a loaded run renders: SearchRun, with whatever slots the page fills.
  renderRun: (payload: SearchRunPayload) => ReactNode
}

// The main pane. Switching runs crossfades (hooks/useRunSwap): the old run fades out, the
// pane is keyed to the new one, which fades in; its query started when the URL changed.
export function RunPane({ runId, renderRun }: RunPaneProps) {
  const pane = useRef<HTMLDivElement>(null)
  const { shown } = useRunSwap(runId, pane)
  return (
    <div ref={pane} key={shown ?? ""} className="run-pane" data-run={shown ?? ""}>
      {shown === null ? <RunEmpty /> : <RunBody runId={shown} renderRun={renderRun} />}
    </div>
  )
}

function RunBody({ runId, renderRun }: { runId: string; renderRun: RunPaneProps["renderRun"] }) {
  const run = useSearchRun(runId)
  // A run that had to load fades its content in over the skeleton; a cached one arrives with the pane.
  const [waited] = useState(run.isPending)
  if (run.data)
    return <div className={waited ? "run-body run-loaded" : "run-body"}>{renderRun(run.data)}</div>
  if (run.error) return <EmptyState data-run-error>{run.error.message}</EmptyState>
  return <RunLoading />
}
