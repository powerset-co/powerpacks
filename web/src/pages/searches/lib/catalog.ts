// The sidebar's filter over the catalog and its recency groups (results.js catalog block,
// network-search-app ConversationSidebar groups).

import type { SearchCard } from "@/types/searches"

import { statusText } from "./copy"

export interface CatalogFilter {
  text: string
  // One version, or null for every run ("All").
  version: string | null
  company: string
  // A status in plain words (statusText), or "" for every status.
  status: string
}

const byName = (a: string, b: string) => a.localeCompare(b, undefined, { sensitivity: "base" })

/** The stamped versions, newest first; unversioned runs only show under "All". */
export function versionsOf(cards: readonly SearchCard[]): string[] {
  return [...new Set(cards.map((card) => card.search_version).filter(Boolean))].sort().reverse()
}

export function companiesOf(cards: readonly SearchCard[]): string[] {
  return [...new Set(cards.map((card) => card.company).filter(Boolean))].sort(byName)
}

export function statusesOf(cards: readonly SearchCard[]): string[] {
  return [...new Set(cards.map((card) => statusText(card.status)))].sort(byName)
}

/** The newest version preselected, as the legacy catalog did. */
export function initialFilter(cards: readonly SearchCard[]): CatalogFilter {
  return { text: "", version: versionsOf(cards)[0] ?? null, company: "", status: "" }
}

export function matches(card: SearchCard, filter: CatalogFilter): boolean {
  const needle = filter.text.trim().toLowerCase()
  return (
    (filter.version === null || card.search_version === filter.version) &&
    (!filter.company || card.company === filter.company) &&
    (!filter.status || statusText(card.status) === filter.status) &&
    (!needle || `${card.title} ${card.company} ${card.run_id}`.toLowerCase().includes(needle))
  )
}

export interface RunGroup {
  title: string
  cards: SearchCard[]
}

// Local midnight `back` calendar days before `date` (calendar days, so DST never shifts a group).
function midnight(date: Date, back = 0): number {
  return new Date(date.getFullYear(), date.getMonth(), date.getDate() - back).getTime()
}

/** Today, Yesterday, This week (from Sunday), then the month; another year adds the year. */
export function recencyTitle(created: string, now: Date): string {
  const date = new Date(created)
  if (!created || Number.isNaN(date.getTime())) return "Earlier"
  const day = midnight(date)
  if (day >= midnight(now)) return "Today"
  if (day >= midnight(now, 1)) return "Yesterday"
  if (day >= midnight(now, now.getDay())) return "This week"
  const month = date.toLocaleDateString("en-US", { month: "long" })
  return date.getFullYear() === now.getFullYear() ? month : `${month} ${date.getFullYear()}`
}

/** Cards keep the catalog's order (newest first); a group sits where its first card does. */
export function groupByRecency(cards: readonly SearchCard[], now: Date): RunGroup[] {
  const groups = new Map<string, SearchCard[]>()
  for (const card of cards) {
    const title = recencyTitle(card.created_at, now)
    const group = groups.get(title)
    if (group) group.push(card)
    else groups.set(title, [card])
  }
  return [...groups].map(([title, grouped]) => ({ title, cards: grouped }))
}
