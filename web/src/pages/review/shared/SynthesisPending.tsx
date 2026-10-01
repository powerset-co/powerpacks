import { SYNTHESIS } from "@/lib/review/copy"

import { EmptyPanel } from "./EmptyPanel"
import { HandoffCopy } from "./HandoffCopy"

// Collected messages have no facts yet, so the screen shows the command to run instead of
// its content.
export function SynthesisPending() {
  return (
    <EmptyPanel title={SYNTHESIS.title}>
      <p>{SYNTHESIS.body}</p>
      <HandoffCopy phrase={SYNTHESIS.command} />
    </EmptyPanel>
  )
}
