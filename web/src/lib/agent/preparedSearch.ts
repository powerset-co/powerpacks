// The search a chat's `search_network_pipeline.py prepare` made, before it runs: the last JSON
// object the command printed (emit, indent=2, so only it opens a line with `{`), known by its
// `primitive` rather than by the command line, which the agent may wrap in a script. The query is
// at its top; the filters, the role intent and the traits sit under `preview`; the local index adds
// the runtime notes. A prepare blocked on its extraction (`blocked_user_action`) carries its
// `runtime_blockers`. The company fast path prints no preview and is no search card.

import { isRecord } from "@/lib/utils"
import type { Entry } from "@/types/agent"

const PRIMITIVE = "search_network_pipeline"

/** A filter's value as the payload holds it: a list of names, a word, a number, a yes/no. */
export type FilterValue = string[] | string | number | boolean

export interface SearchFilter {
  /** The payload's key, as an edit names it back to the agent: `metro_areas`. */
  key: string
  value: FilterValue
}

/** One of the expand step's traits: `VP of Finance`, `current` (or `past`, `all`), `role` when the step said. */
export interface SearchTrait {
  value: string
  temporal: string
  meaning?: string
}

export interface PreparedSearch {
  query: string
  /** The preview's filters, then its role intent (`semantic_query`, `bm25_queries`, …), in order. */
  filters: SearchFilter[]
  /** Empty when the preview carries none, as replies from older builds. */
  traits: SearchTrait[]
  notes: string[]
  /** Why the search cannot run as prepared; set only on a blocked prepare, which is never executed. */
  blockers: string[]
  /** Prepared on the local index (`mode: local_duckdb`): no investors or interaction filters. */
  local: boolean
}

/** The value as the payload holds it, or null for anything empty or of another shape. */
function filterValue(value: unknown): FilterValue | null {
  if (Array.isArray(value)) {
    const names = value.filter((item): item is string => typeof item === "string" && item.trim() !== "")
    return names.length > 0 ? names : null
  }
  if (typeof value === "string") return value.trim() === "" ? null : value
  if (typeof value === "number" || typeof value === "boolean") return value
  return null
}

function searchFilters(...sources: unknown[]): SearchFilter[] {
  const filters: SearchFilter[] = []
  for (const source of sources) {
    if (!isRecord(source)) continue
    for (const [key, raw] of Object.entries(source)) {
      const value = filterValue(raw)
      if (value !== null) filters.push({ key, value })
    }
  }
  return filters
}

/** The traits that carry a value and a temporal, with the meaning when the step gave one. */
function searchTraits(value: unknown): SearchTrait[] {
  const traits: SearchTrait[] = []
  if (!Array.isArray(value)) return traits
  for (const trait of value) {
    if (isRecord(trait) && typeof trait.value === "string" && typeof trait.temporal === "string")
      traits.push(
        typeof trait.meaning === "string"
          ? { value: trait.value, temporal: trait.temporal, meaning: trait.meaning }
          : { value: trait.value, temporal: trait.temporal },
      )
  }
  return traits
}

/** One line per item: the runtime notes, the blockers. */
function lines(value: unknown): string[] {
  return Array.isArray(value) ? value.map(String) : []
}

/** What the search primitive printed last, or null for any other command or output still arriving. */
function printed(entry: Entry): Record<string, unknown> | null {
  if (entry.kind !== "command") return null
  let value: unknown
  try {
    value = JSON.parse(entry.output.slice(entry.output.lastIndexOf("\n{") + 1))
  } catch {
    return null
  }
  return isRecord(value) && value.primitive === PRIMITIVE ? value : null
}

/** True once the command that ran the search printed its result (its summary; a prepare prints a preview). */
export function ranSearch(entry: Entry): boolean {
  const value = printed(entry)
  return value !== null && !("preview" in value) && "summary" in value
}

/** The command's search, or null for any other command, a fast path, or output still arriving. */
export function preparedSearch(entry: Entry): PreparedSearch | null {
  const value = printed(entry)
  if (value === null || typeof value.query !== "string" || !isRecord(value.preview)) return null
  const preview = value.preview
  if (!isRecord(preview.filters)) return null

  const blockers = value.status === "blocked_user_action" ? lines(preview.runtime_blockers) : []
  return {
    query: value.query,
    filters: searchFilters(preview.filters, preview.role_title_intent),
    traits: searchTraits(preview.traits),
    notes: lines(preview.runtime_notes),
    blockers,
    local: value.mode === "local_duckdb",
  }
}
