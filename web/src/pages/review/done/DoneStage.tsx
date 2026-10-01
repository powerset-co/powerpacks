export interface DoneStageProps {
  /** Identities checked (`progress.linkedin_done`). */
  checked: number
  /** People rejected (`progress.rejected`). */
  rejected: number
}

// Stub: the done stage ("All set", the counts, the go-back handoff) replaces this body.
export function DoneStage({ checked, rejected }: DoneStageProps) {
  return <div className="empty-state done" data-checked={checked} data-rejected={rejected} />
}
