import { useState } from "react"

import type { EnrichmentJob } from "@/lib/review/sync"
import type { EnrichmentPanel } from "@/types/review"

import { EmptyPanel } from "../shared/EmptyPanel"
import { TITLE } from "./copy"
import { barPercent, jobProgress, panelProgress } from "./progress"

interface RunningPanelProps {
  /** The panel as the server drew it: the page load's, or the approve answer's. */
  panel: EnrichmentPanel
  /** The running job's latest event. One already here when the panel is drawn is older than
   *  the panel, so only a later one counts. */
  job: EnrichmentJob | null
}

// "Enriching Contacts": the count line and the bar, rewritten in place by each event of the
// running job. No button: nothing can be approved twice.
export function RunningPanel({ panel, job }: RunningPanelProps) {
  const [shown, setShown] = useState({ job, progress: panelProgress(panel) })
  if (job && job !== shown.job) setShown({ job, progress: jobProgress(shown.progress, job) })
  const { progress } = shown

  return (
    <EmptyPanel title={TITLE.running} className="enrich-state">
      <p>{progress.text}</p>
      <div
        className="enrich-progress"
        role="progressbar"
        aria-valuemin={0}
        aria-valuemax={progress.total}
        aria-valuenow={progress.completed}
      >
        <div className="enrich-progress-fill" style={{ width: `${barPercent(progress)}%` }} />
      </div>
    </EmptyPanel>
  )
}
