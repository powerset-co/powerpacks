// The one filter pass over the rows: the tab (one decision, or all), the search text and the
// held facets, plus the relationship counts the filter offers.

import { worthTab, type Person } from "@/types/people"

import { facetOf, HIDES, sortRows } from "./facets"
import type { PeopleView } from "./view"

export interface FilterResult {
  matching: Person[]
  // How many in the tab hold each relationship kind, whatever the search and filters.
  kinds: Map<string, number>
}

export function filterRows(
  rows: readonly Person[],
  { tab, filters, hide, text, sort }: PeopleView,
): FilterResult {
  const needle = text.trim().toLowerCase()
  // A hide that would leave nobody (LinkedIn-only people on a LinkedIn-only install) does not apply.
  const hidden = HIDES.filter(({ key, hides }) => hide.has(key) && rows.some((row) => !hides(row)))
  const active = [...filters]
    .filter(([, values]) => values.size)
    .map(([key, values]) => [facetOf(key), values] as const)
  const kinds = new Map<string, number>()
  const matching: Person[] = []
  for (const row of rows) {
    if (tab !== "all" && worthTab(row.worth) !== tab) continue
    if (row.relationship_kind) kinds.set(row.relationship_kind, (kinds.get(row.relationship_kind) ?? 0) + 1)
    if (needle && !row.search.includes(needle)) continue
    if (hidden.some(({ hides }) => hides(row))) continue
    if (active.every(([facet, values]) => facet.get(row).some((value) => values.has(value))))
      matching.push(row)
  }
  return { matching: sortRows(matching, sort), kinds }
}
