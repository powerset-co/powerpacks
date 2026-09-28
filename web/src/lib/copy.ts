// Words both pages use: counts and month-year dates.

// Nouns that take "es": search, match, box.
const SIBILANT = /(s|sh|ch|x)$/

export function plural(count: number, noun: string): string {
  const many = noun === "person" ? "people" : SIBILANT.test(noun) ? `${noun}es` : `${noun}s`
  return `${count.toLocaleString()} ${count === 1 ? noun : many}`
}

/** "12 results", or "12 of 125 results" while a filter hides some. */
export function countOf(shown: number, total: number, noun: string): string {
  return shown === total ? plural(total, noun) : `${shown.toLocaleString()} of ${plural(total, noun)}`
}

/** A date, or null when the value is not one. */
export function parseDate(value: string): Date | null {
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? null : date
}

/** "Sep 2026" (people.js formatDate, rendering.py _month_year); an unparseable value as written. */
export function monthYear(value: string): string {
  const date = parseDate(value)
  if (!date) return value
  return date.toLocaleDateString("en-US", { month: "short", year: "numeric" })
}

const DAY_MS = 86_400_000

/** "today", "yesterday", "5 days ago"; "never" for no date. */
export function ago(value: string | null, now: number = Date.now()): string {
  if (!value) return "never"
  const days = Math.floor((now - new Date(value).getTime()) / DAY_MS)
  if (days <= 0) return "today"
  if (days === 1) return "yesterday"
  return `${days} days ago`
}

/** "Sep 28, 2:32 PM PDT" in the viewer's own timezone; "never" for no date. */
export function stamp(value: string | null): string {
  if (!value) return "never"
  return new Date(value).toLocaleString("en-US", {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
    timeZoneName: "short",
  })
}
