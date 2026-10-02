import type { LinkedinFinished } from "@/types/review"

import { EmptyPanel } from "../shared/EmptyPanel"
import { GoBack } from "../shared/GoBack"
import { decisionsSaved, FINISHED, RESEARCH_RUNNING } from "./copy"

interface FinishedPanelProps {
  finished: LinkedinFinished
}

// The queue has nothing left to show: hand back to Codex. A re-research still out settles its
// person in the background, and the agent waits for it before going on.
export function FinishedPanel({ finished }: FinishedPanelProps) {
  return (
    <EmptyPanel title={FINISHED.title}>
      <p>{decisionsSaved(finished.linkedin_done)}</p>
      {finished.retargets_in_flight ? <p>{RESEARCH_RUNNING}</p> : null}
      <GoBack />
    </EmptyPanel>
  )
}
