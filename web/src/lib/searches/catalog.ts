// The sidebar's search over the catalog, kept for the tab (sessionStorage), and its recency
// groups (network-search-app ConversationSidebar groups).

import { readStored, writeStored } from "@/lib/storage"
import { isRecord } from "@/lib/utils"
import type { SearchCard } from "@/types/searches"

export interface CatalogFilter {
  text: string
}

const FILTER_KEY = "powerpacks:search-catalog-filter:v2"

export const NO_FILTER: CatalogFilter = { text: "" }

/** A saved filter, or null unless it has the shape. */
export function parseCatalogFilter(raw: unknown): CatalogFilter | null {
  if (!isRecord(raw) || typeof raw.text !== "string") return null
  return { text: raw.text }
}

/** The search this tab last typed (a visit to People and back keeps it), else none. */
export function readCatalogFilter(): CatalogFilter {
  return readStored("session", FILTER_KEY, parseCatalogFilter) ?? NO_FILTER
}

export function writeCatalogFilter(filter: CatalogFilter): void {
  writeStored("session", FILTER_KEY, filter)
}

/** Title, company and run id, case-insensitively. */
export function matches(card: SearchCard, filter: CatalogFilter): boolean {
  const needle = filter.text.trim().toLowerCase()
  return !needle || `${card.title} ${card.company} ${card.run_id}`.toLowerCase().includes(needle)
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
