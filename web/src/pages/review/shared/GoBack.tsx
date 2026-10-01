import { HANDOFF } from "@/lib/review/copy"

import { HandoffCopy } from "./HandoffCopy"

// templates/go_back.html.j2: the end-of-review handoff, the phrase to give Codex.
export function GoBack() {
  return (
    <>
      <p className="handoff-note">{HANDOFF.note}</p>
      <HandoffCopy phrase={HANDOFF.phrase} />
    </>
  )
}
