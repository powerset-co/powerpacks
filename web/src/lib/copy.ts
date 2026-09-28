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
