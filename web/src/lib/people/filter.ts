// The one filter/count pass over the rows, ported 1:1 from people.js `filterRows`.

import type { Decision, Person } from "@/types/people";

import { FACET_BY_KEY, FACETS, QUICK, sortRows, type FacetDef, type Sort } from "./facets";

export interface FilterView {
  tab: Decision;
  filters: ReadonlyMap<string, ReadonlySet<string>>;
  text: string;
  sort: Sort;
}

export interface FilterResult {
  matching: Person[];
  counts: Map<string, Map<string, number>>;
  quickCounts: number[];
}

function facetOf(key: string): FacetDef {
  const facet = FACET_BY_KEY.get(key);
  if (!facet) throw new Error(`unknown facet: ${key}`);
  return facet;
}

export function filterRows(rows: readonly Person[], { tab, filters, text, sort }: FilterView): FilterResult {
  const needle = text.trim().toLowerCase();
  const active = [...filters].filter(([, values]) => values.size).map(([key, values]) => [facetOf(key), values] as const);
  const buckets = FACETS.map((facet) => [facet, new Map<string, number>()] as const);
  const quickCounts = QUICK.map(() => 0);
  const matching: Person[] = [];
  for (const row of rows) {
    if (row.share !== tab) continue;
    QUICK.forEach((quick, position) => {
      const hit = Object.entries(quick.set)
        .every(([key, values]) => facetOf(key).get(row).some((value) => values.includes(value)));
      if (hit) quickCounts[position] = (quickCounts[position] ?? 0) + 1;
    });
    if (needle && !row.search.includes(needle)) continue;
    let failed = 0;
    let failedKey = "";
    for (const [facet, values] of active) {
      if (facet.get(row).some((value) => values.has(value))) continue;
      failed += 1;
      failedKey = facet.key;
      if (failed > 1) break;
    }
    if (failed === 0) matching.push(row);
    if (failed > 1) continue;
    // A facet's counts ignore its own selection so the other choices stay discoverable.
    for (const [facet, bucket] of buckets) {
      if (failed === 1 && facet.key !== failedKey) continue;
      for (const value of facet.get(row)) bucket.set(value, (bucket.get(value) ?? 0) + 1);
    }
  }
  const counts = new Map(buckets.map(([facet, bucket]) => [facet.key, bucket]));
  return { matching: sortRows(matching, sort), counts, quickCounts };
}
