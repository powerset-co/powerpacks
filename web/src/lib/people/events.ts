import type { FactEvent } from "@/types/people"

/** The timeline reads newest first; an event without a date goes last. */
export function newestFirst(events: FactEvent[]): FactEvent[] {
  return [...events].sort((a, b) => {
    if (!a.date || !b.date) return a.date ? -1 : b.date ? 1 : 0
    return b.date.localeCompare(a.date)
  })
}
