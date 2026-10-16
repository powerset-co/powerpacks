// The search a chat's `search_network_pipeline.py prepare` command made, before it runs: the
// last JSON object the command printed (emit, indent=2, so only it opens a line with `{`). The
// query is at its top and the filters and the traits under `preview`; the local index adds the
// pool estimate and the runtime notes. A prepare blocked on its extraction (`blocked_user_action`)
// carries its `runtime_blockers`. The company fast path prints no filters and is no search card.

import { isRecord } from "@/lib/utils"
import type { Entry } from "@/types/agent"

const PREPARE = /search_network_pipeline\.py\s+prepare\b/

export interface SearchFilter {
  /** The preview's key, as an edit names it back to the agent: `metro_areas`. */
  key: string
  label: string
  value: string
}

/** One of the expand step's traits: `VP of Finance`, `current` (or `past`, `all`). */
export interface SearchTrait {
  value: string
  temporal: string
}

export interface PreparedSearch {
  query: string
  filters: SearchFilter[]
  /** Empty when the preview carries none, as replies from older builds. */
  traits: SearchTrait[]
  /** How many people in the local index the filters match; null when the backend gives no count. */
  pool: { matched: number; total: number } | null
  notes: string[]
  /** Why the search cannot run as prepared; set only on a blocked prepare, which is never executed. */
  blockers: string[]
}

/** `metro_areas` → `Metro areas`. */
function filterLabel(key: string): string {
  const words = key.replaceAll("_", " ")
  return `${words.charAt(0).toUpperCase()}${words.slice(1)}`
}

/** A list joined with ", "; a word, number or yes/no as itself; anything empty as "". */
function filterValue(value: unknown): string {
  if (Array.isArray(value)) return value.map(String).join(", ")
  const plain = typeof value === "string" || typeof value === "number" || typeof value === "boolean"
  return plain ? String(value) : ""
}

/** The traits that carry a value and a temporal. */
function searchTraits(value: unknown): SearchTrait[] {
  const traits: SearchTrait[] = []
  if (!Array.isArray(value)) return traits
  for (const trait of value) {
    if (isRecord(trait) && typeof trait.value === "string" && typeof trait.temporal === "string")
      traits.push({ value: trait.value, temporal: trait.temporal })
  }
  return traits
}

/** One line per item: the runtime notes, the blockers. */
function lines(value: unknown): string[] {
  return Array.isArray(value) ? value.map(String) : []
}

/** The command's search, or null for any other command, a fast path, or output still arriving. */
export function preparedSearch(entry: Entry): PreparedSearch | null {
  if (entry.kind !== "command" || !PREPARE.test(entry.command)) return null
  let printed: unknown
  try {
    printed = JSON.parse(entry.output.slice(entry.output.lastIndexOf("\n{") + 1))
  } catch {
    return null
  }
  if (!isRecord(printed) || typeof printed.query !== "string" || !isRecord(printed.preview)) return null
  const preview = printed.preview
  if (!isRecord(preview.filters)) return null

  const filters: SearchFilter[] = []
  for (const [key, value] of Object.entries(preview.filters)) {
    const shown = filterValue(value)
    if (shown) filters.push({ key, label: filterLabel(key), value: shown })
  }
  const estimate = preview.pool_estimate
  const pool =
    isRecord(estimate) &&
    typeof estimate.matched_people === "number" &&
    typeof estimate.total_people === "number"
      ? { matched: estimate.matched_people, total: estimate.total_people }
      : null
  const blockers = printed.status === "blocked_user_action" ? lines(preview.runtime_blockers) : []
  return {
    query: printed.query,
    filters,
    traits: searchTraits(preview.traits),
    pool,
    notes: lines(preview.runtime_notes),
    blockers,
  }
}
