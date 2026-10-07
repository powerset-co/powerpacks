// The review page's words that more than one screen shows; a stage's own live beside the stage.

export const BRAND = "POWERPACKS"

/** The browser tab: "Add People · Powerpacks". */
export function documentTitle(title: string): string {
  return `${title} · Powerpacks`
}

/** The check a finished panel draws. */
export const COMPLETE_MARK = "✓"

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

export const SYNTHESIS = {
  title: "Synthesis has not run",
  body: "Collected messages have no facts yet. Go back to Codex and run:",
  command: "bin/deep-context-v2 run",
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

/** The screen's own load failed. */
export const LOAD_FAILED = "Could not load the review"
