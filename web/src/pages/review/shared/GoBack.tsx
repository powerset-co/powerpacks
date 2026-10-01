import { HANDOFF } from "@/lib/review/copy"

import { HandoffCopy } from "./HandoffCopy"

// The end-of-review handoff: the phrase to give Codex.
export function GoBack() {
  return (
    <>
      <p className="handoff-note">{HANDOFF.note}</p>
      <HandoffCopy phrase={HANDOFF.phrase} />
    </>
  )
}
