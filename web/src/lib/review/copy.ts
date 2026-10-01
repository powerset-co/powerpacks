// The review page's words, as the old page wrote them (templates/*.html.j2, reconcile_review.js).

import type { ReviewView } from "@/types/review"

export const BRAND = "POWERPACKS"

/** The browser tab: "Add People · Powerpacks". */
export function documentTitle(title: string): string {
  return `${title} · Powerpacks`
}

export const STEPPER_LABEL = "Progress"

/** The name a card shows when nobody has one. */
export const UNNAMED = "This person"

export const FACT = {
  contact: "Contact",
  summary: "Summary",
  location: "Location",
  work: "Work",
  education: "Education",
} as const

export const VIEW_LINKEDIN = "View LinkedIn"

export const SHOW_FEWER = "show fewer"
export function showMore(hidden: number): string {
  return `+ show ${hidden} more`
}

export function moreLabels(tooltip: string): string {
  return `More labels: ${tooltip}`
}

export const DOSSIER = {
  loading: "Loading…",
  missing: "No details found",
  failed: "Could not load details",
} as const

export const SCROLL_DOWN = "Scroll down"

export const CAROUSEL = { previous: "Previous", next: "Next" } as const

/** The words under the check when a stage finishes. LinkedIn's check stands alone: the
 *  finished screen it opens carries the words. */
export const STAGE_DONE: Readonly<Record<Exclude<ReviewView, "done">, string>> = {
  worth: "People Reviewed",
  enrich: "Contacts Enriched",
  linkedin: "",
}

/** Under the stage check while the next screen loads. */
export const PREPARING_NEXT = "Preparing Next Stage"

export const SYNTHESIS = {
  title: "Synthesis has not run",
  body: "Collected messages have no facts yet. Go back to Codex and run:",
  command: "bin/deep-context dry",
} as const

export const HANDOFF = {
  note: "Review complete — go back to Codex.",
  phrase: "Review complete, continue",
  copy: "Copy",
} as const

export const TOAST = {
  added: "Added",
  rejected: "Rejected",
  saved: "Saved",
  skipped: "Skipped",
  applied: "Applied",
  approved: "Approved",
  copied: "Copied",
  queued: "Queued for re-research — moving on",
  alreadyDecided: "Already decided",
  cardFailed: "Could not load card",
} as const

export function copyFailed(phrase: string): string {
  return `Copy failed — type: ${phrase}`
}

/** A POST that failed without a body. */
export const SAVE_FAILED = "Could not save"

/** The screen's own load failed (the old page was a document: the browser said so). */
export const LOAD_FAILED = "Could not load the review"
