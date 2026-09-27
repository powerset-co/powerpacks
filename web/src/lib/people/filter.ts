// The one filter/count pass over the rows, ported 1:1 from people.js `filterRows`.

import type { Person } from "@/types/people"

import { facetOf, FACETS, QUICK, setEntries, sortRows, type FacetKey } from "./facets"
import type { PeopleView } from "./view"

export interface FilterResult {
  matching: Person[]
  counts: Map<FacetKey, Map<string, number>>
  quickCounts: number[]
}

export function filterRows(rows: readonly Person[], { tab, filters, text, sort }: PeopleView): FilterResult {
  const needle = text.trim().toLowerCase()
  const active = [...filters]
    .filter(([, values]) => values.size)
    .map(([key, values]) => [facetOf(key), values] as const)
  const buckets = FACETS.map((facet) => [facet, new Map<string, number>()] as const)
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
    let failed = 0
    let failedKey = ""
    for (const [facet, values] of active) {
      if (facet.get(row).some((value) => values.has(value))) continue
      failed += 1
      failedKey = facet.key
      if (failed > 1) break
    }
    if (failed === 0) matching.push(row)
    if (failed > 1) continue
    // A facet's counts ignore its own selection so the other choices stay discoverable.
    for (const [facet, bucket] of buckets) {
      if (failed === 1 && facet.key !== failedKey) continue
      for (const value of facet.get(row)) bucket.set(value, (bucket.get(value) ?? 0) + 1)
    }
  }
  const counts = new Map(buckets.map(([facet, bucket]) => [facet.key, bucket]))
  return { matching: sortRows(matching, sort), counts, quickCounts }
}
