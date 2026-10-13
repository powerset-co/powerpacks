// The one filter pass over the rows: the tab, the search text and the held facets, plus the quick-filter counts.

import type { Person } from "@/types/people"

import { facetOf, QUICK, setEntries, sortRows } from "./facets"
import type { PeopleView } from "./view"

export interface FilterResult {
  matching: Person[]
  quickCounts: number[]
}

export function filterRows(rows: readonly Person[], { tab, filters, text, sort }: PeopleView): FilterResult {
  const needle = text.trim().toLowerCase()
  const active = [...filters]
    .filter(([, values]) => values.size)
    .map(([key, values]) => [facetOf(key), values] as const)
  const quickCounts = QUICK.map(() => 0)
  const matching: Person[] = []
  for (const row of rows) {
    if (row.share !== tab) continue
    QUICK.forEach((quick, position) => {
      const hit = setEntries(quick.set).every(([key, values]) =>
        facetOf(key)
          .get(row)
          .some((value) => values.includes(value)),
      )
      if (hit) quickCounts[position] = (quickCounts[position] ?? 0) + 1
    })
    if (needle && !row.search.includes(needle)) continue
    if (active.every(([facet, values]) => facet.get(row).some((value) => values.has(value))))
      matching.push(row)
  }
  return { matching: sortRows(matching, sort), quickCounts }
}
