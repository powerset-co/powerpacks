/**
 * Which row the drawer shows after the one at `index` (key `done`) was labeled or scored,
 * given the list as it is now. The row left the list (a label moved it to another tab): the
 * one now at the same index, or the last one when the list got shorter than that, which walks
 * backwards from the end. The row stayed: the next one down, or nobody at the end. Null means
 * nobody; the page decides whether that keeps the drawer or closes it.
 */
export function nextOpenIndex<T>(
  rows: readonly T[],
  keyOf: (row: T) => string,
  done: string,
  index: number,
): number | null {
  const stillAt = rows.findIndex((row) => keyOf(row) === done)
  if (stillAt >= 0) return stillAt + 1 < rows.length ? stillAt + 1 : null
  if (!rows.length) return null
  return Math.min(index, rows.length - 1)
}
