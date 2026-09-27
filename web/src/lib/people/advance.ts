import type { Person } from "@/types/people"

/**
 * Who the drawer shows after the person at `index` (id `labeled`) was labeled, given the
 * list as it is now. The person left the list (the label moved them to another tab): the
 * one now at the same index, or the last one when the list got shorter than that, which
 * walks backwards from the end. The person stayed: the next one down, or nobody at the end.
 * Null means nobody: keep the drawer where it is, or close it when the list is empty.
 */
export function nextOpenIndex(rows: readonly Person[], labeled: string, index: number): number | null {
  const stillAt = rows.findIndex((row) => row.parent_id === labeled)
  if (stillAt >= 0) return stillAt + 1 < rows.length ? stillAt + 1 : null
  if (!rows.length) return null
  return Math.min(index, rows.length - 1)
}
