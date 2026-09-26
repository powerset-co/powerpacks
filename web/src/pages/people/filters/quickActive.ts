import type { QuickFilter } from "@/lib/people/facets";

/** A quick filter is on when the held facets are exactly its set, nothing more. */
export function quickActive(quick: QuickFilter, filters: ReadonlyMap<string, ReadonlySet<string>>): boolean {
  const entries = Object.entries(quick.set);
  const heldKeys = [...filters].filter(([, values]) => values.size).length;
  return heldKeys === entries.length && entries.every(([key, values]) => {
    const held = filters.get(key);
    return held !== undefined && held.size === values.length && values.every((value) => held.has(value));
  });
}
