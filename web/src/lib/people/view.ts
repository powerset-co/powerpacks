// The People view (tab, facet selections, search text, sort) and its saved form, kept in
// sessionStorage under the legacy people.js key so a reload keeps the view.

import { readStored, writeStored } from "@/lib/storage"
import { isRecord } from "@/lib/utils"
import { ORDER, type Decision } from "@/types/people"

import { isFacetKey, isSortKey, type FacetFilters, type FacetKey, type Sort } from "./facets"

const VIEW_KEY = "powerpacks:people-filters:v2"

export interface PeopleView {
  tab: Decision
  filters: FacetFilters
  text: string
  sort: Sort
}

export const DEFAULT_VIEW: PeopleView = {
  tab: "confirm",
  filters: new Map(),
  text: "",
  sort: { key: "name", dir: 1 },
}

const isStrings = (raw: unknown): raw is string[] =>
  Array.isArray(raw) && raw.every((value) => typeof value === "string")

function parseSort(raw: unknown): Sort | null {
  if (!isRecord(raw) || !isSortKey(raw.key) || (raw.dir !== 1 && raw.dir !== -1)) return null
  return { key: raw.key, dir: raw.dir }
}

function parseFilters(raw: unknown): PeopleView["filters"] | null {
  if (!isRecord(raw)) return null
  const filters = new Map<FacetKey, ReadonlySet<string>>()
  for (const [key, values] of Object.entries(raw)) {
    if (!isFacetKey(key) || !isStrings(values)) return null
    filters.set(key, new Set(values))
  }
  return filters
}

/** The saved view, or null unless every part names a real tab, facet and sort. */
export function parseView(raw: unknown): PeopleView | null {
  if (!isRecord(raw) || typeof raw.text !== "string") return null
  const tab = ORDER.find((decision) => decision === raw.tab)
  const filters = parseFilters(raw.filters)
  const sort = parseSort(raw.sort)
  if (!tab || !filters || !sort) return null
  return { tab, filters, text: raw.text, sort }
}

export function readView(): PeopleView | null {
  return readStored("session", VIEW_KEY, parseView)
}

export function writeView({ tab, filters, text, sort }: PeopleView): void {
  const stored = Object.fromEntries([...filters].map(([key, values]) => [key, [...values]]))
  writeStored("session", VIEW_KEY, { tab, filters: stored, text, sort })
}
