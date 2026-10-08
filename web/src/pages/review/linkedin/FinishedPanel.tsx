import { EmptyPanel } from "../shared/EmptyPanel"
import { FINISHED } from "./copy"

// The queue has nothing left to show. The server has recorded the review as complete; the agent
// sees that and rebuilds the index, so the page only says so.
export function FinishedPanel() {
  return (
    <EmptyPanel title={FINISHED.title}>
      <p className="handoff-note">{FINISHED.note}</p>
      <p className="handoff-note">{FINISHED.hint}</p>
    </EmptyPanel>
  )
}
