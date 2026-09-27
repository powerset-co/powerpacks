// The Searches page's words for machine values, ported from rendering.py.

import { parseDate } from "@/lib/copy"
import type { SearchCard } from "@/types/searches"

// rendering.py _status_text: a finished run reads "Search complete".
const COMPLETE = new Set(["awaiting_diagnosis", "completed"])

export function isComplete(status: string): boolean {
  return COMPLETE.has(status)
}

export function statusText(status: string): string {
  if (isComplete(status)) return "Search complete"
  const words = status.replaceAll("_", " ")
  return words.charAt(0).toUpperCase() + words.slice(1).toLowerCase()
}

/** rendering.py _date: "Sep 26, 2026"; an unparseable value as written. */
export function runDate(value: string): string {
  if (!value) return "Unknown date"
  const date = parseDate(value)
  if (!date) return value
  return date.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" })
}

export function money(value: number): string {
  return `$${value.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
}

/** rendering.py _catalog_row's people tooltip: pinned, then how many scored 5, 4 and 3 overall. */
export function peopleCounts(card: SearchCard): string {
  const counts: readonly (readonly [string, number])[] = [
    ["5", card.score_5],
    ["4", card.score_4],
    ["3", card.score_3],
  ]
  const scores = counts.map(([score, count]) => `${score}/5: ${count.toLocaleString("en-US")}`)
  return [`Pinned ${card.pinned.toLocaleString("en-US")}`, `Overall ${scores.join(", ")}`].join(" · ")
}
