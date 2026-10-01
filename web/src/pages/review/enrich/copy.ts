// The Enrich stage's words (templates/enrichment.html.j2, reconcile_review.js `renderJobProgress`).

import type { EnrichmentMode } from "@/types/review"

/** The panel's title in each of its states. */
export const TITLE: Readonly<Record<EnrichmentMode, string>> = {
  preparing: "Preparing Enrichment",
  approval: "Ready to Enrich",
  running: "Enriching Contacts",
  completed: "Contacts Enriched",
  failed: "Enrichment Paused",
}

export const CONTINUE = "Continue"

/** Research lookups finished, of those planned. */
export function lookupsComplete(completed: number, total: number): string {
  return `${completed} of ${total} complete`
}

/** Found LinkedIns the judge has checked, of those it will. */
export function profilesChecked(done: number, total: number): string {
  return `${done} of ${total} checked`
}
