// The Searches page's words for machine values, ported from rendering.py.

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

function parsed(value: string): Date | null {
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? null : date
}

/** rendering.py _date: "Sep 26, 2026"; an unparseable value as written. */
export function runDate(value: string): string {
  if (!value) return "Unknown date"
  const date = parsed(value)
  if (!date) return value
  return date.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" })
}

/** rendering.py _month_year: "Sep 2026"; an unparseable value as written. */
export function monthYear(value: string): string {
  const date = parsed(value)
  if (!date) return value
  return date.toLocaleDateString("en-US", { month: "short", year: "numeric" })
}

export function money(value: number): string {
  return `$${value.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
}
