import { EmptyPanel } from "../shared/EmptyPanel"
import { GoBack } from "../shared/GoBack"
import { ALL_SET, reviewTally } from "./copy"

export interface DoneStageProps {
  /** Identities checked (`progress.linkedin_done`). */
  checked: number
  /** People rejected (`progress.rejected`). */
  rejected: number
}

// The Done stage (server.py `full_page`): the check, "All set", what the review came to, and
// the phrase to take back to Codex.
export function DoneStage({ checked, rejected }: DoneStageProps) {
  return (
    <EmptyPanel mark title={ALL_SET} className="done">
      <p>{reviewTally(checked, rejected)}</p>
      <GoBack />
    </EmptyPanel>
  )
}
