import type { LinkedinFinished } from "@/types/review"

import { EmptyPanel } from "../shared/EmptyPanel"
import { GoBack } from "../shared/GoBack"
import { decisionsSaved, FINISHED, researchRunning } from "./copy"

interface FinishedPanelProps {
  finished: LinkedinFinished
}

// The queue has nothing left to show. With every person decided it hands back to Codex;
// while re-research is still out it says so, and the stage reads the queue again by itself.
export function FinishedPanel({ finished }: FinishedPanelProps) {
  return (
    <EmptyPanel title={FINISHED.title}>
      <p>{decisionsSaved(finished.linkedin_done)}</p>
      {finished.retargets_in_flight ? <p>{researchRunning(finished.retargets_in_flight)}</p> : null}
      {finished.linkedin_complete ? <GoBack /> : null}
    </EmptyPanel>
  )
}
