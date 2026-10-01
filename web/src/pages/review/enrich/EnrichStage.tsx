import type { EnrichmentJob } from "@/lib/review/sync"
import type { EnrichmentPanel } from "@/types/review"

export interface EnrichStageProps {
  /** The panel as the screen loaded it. */
  enrichment: EnrichmentPanel
  /** The running enrichment's latest numbers from /api/events; null until one arrives. */
  job: EnrichmentJob | null
}

// Stub: the enrich stage (the panel's five states, approve, live progress, Continue) replaces this body.
export function EnrichStage({ enrichment, job }: EnrichStageProps) {
  return <div className="enrich-state" data-mode={enrichment.mode} data-job={job?.status} />
}
