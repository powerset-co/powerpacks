// Immutable set helpers shared by selection, filters, facets and drawer sections.

export const EMPTY: ReadonlySet<string> = new Set();

/** A copy of `set` with `value` added when `on`, removed otherwise; `on` defaults to flipping it. */
export function toggled<T>(set: ReadonlySet<T>, value: T, on = !set.has(value)): ReadonlySet<T> {
  const next = new Set(set);
  if (on) next.add(value);
  else next.delete(value);
  return next;
}
