import { EmptyPanel } from "../shared/EmptyPanel"
import { GoBack } from "../shared/GoBack"
import { FINISHED } from "./copy"

// The queue has nothing left to show: hand back to Codex.
export function FinishedPanel() {
  return (
    <EmptyPanel title={FINISHED.title}>
      <GoBack />
    </EmptyPanel>
  )
}
