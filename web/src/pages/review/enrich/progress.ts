// What the running panel shows, the count line and the bar (rendering.py `render_enrichment`),
// and what a running job's event does to them (reconcile_review.js `renderJobProgress`).

import type { EnrichmentJob } from "@/lib/review/sync"
import type { EnrichmentPanel } from "@/types/review"

import { lookupsComplete, profilesChecked } from "./copy"

/** The judge is checking the found LinkedIns: the job's own counts no longer say lookups. */
const JUDGING = "judging_retargets"

export interface EnrichProgress {
  /** The line under the title. */
  text: string
  /** The bar: lookups finished, of those planned. */
  completed: number
  total: number
}

/** The panel as the server drew it. */
export function panelProgress(panel: EnrichmentPanel): EnrichProgress {
  const { completed, total } = panel
  return { text: lookupsComplete(completed, total), completed, total }
}

/**
 * A running job's event over what is on show. Research rewrites the line and the bar. Judging
 * rewrites the line and leaves the bar where it was, and so does a job with nothing planned.
 */
export function jobProgress(shown: EnrichProgress, job: EnrichmentJob): EnrichProgress {
  if (job.phase === JUDGING) {
    const done = job.progress?.phase_done ?? 0
    return { ...shown, text: profilesChecked(done, job.progress?.phase_total ?? 0) }
  }

  const total = job.counts?.total ?? 0
  const completed = Math.min(total, job.counts?.completed ?? 0)
  const text = lookupsComplete(completed, total)
  return total ? { text, completed, total } : { ...shown, text }
}

/** The fill's width in whole percent; 0 with nothing planned. */
export function barPercent({ completed, total }: EnrichProgress): number {
  return total ? Math.round((completed / total) * 100) : 0
}
