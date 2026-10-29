// Facets, sorting and the tag toggle, ported from people.js. A held facet keeps the rows with one
// of its values, always within the selected decision tab.

import { must } from "@/lib/must"
import { LAST, WARMTH, type LastBucket, type Person, type WarmthBucket } from "@/types/people"

import { label, sentence, type TextKind } from "./copy"

const YEAR = 365

export { LAST, WARMTH }

export function lastBucket(days: number | null): LastBucket {
  if (days === null) return LAST[3]
  if (days < YEAR) return LAST[0]
  if (days <= 2 * YEAR) return LAST[1]
  return LAST[2]
}

/** The warmth band, or "" when the person has no warmth score. */
export function warmthBucket(value: number | null): WarmthBucket | "" {
  if (value === null) return ""
  return must(WARMTH[Math.min(3, Math.floor(value))], "warmth band")
}

export type FacetKey =
  | "reason"
  | "worth"
  | "relationship_kind"
  | "last"
  | "channels"
  | "linkedin"
  | "logbook"
  | "worth_source"
  | "tags"
  | "labels"
  | "function"
  | "warmth"
  | "direction"
  | "intro_source"
  | "evidence"

/** The values held per facet: the view's filters. */
export type FacetFilters = ReadonlyMap<FacetKey, ReadonlySet<string>>

/** `get` returns the row's values for the facet; `words` names the TEXT map that words its values. */
export interface FacetDef {
  key: FacetKey
  label: string
  get: (row: Person) => string[]
  words?: TextKind
}

const one = (value: string): string[] => (value ? [value] : [])

// What each facet reads from a row: the relationship filter and the URL view use these.
export const FACETS: readonly FacetDef[] = [
  { key: "reason", label: "Reason", get: (r) => [r.reason], words: "reason" },
  {
    key: "worth",
    label: "Decision",
    get: (r) => [r.worth || "unjudged"],
    words: "worth",
  },
  { key: "relationship_kind", label: "Relationship", get: (r) => one(r.relationship_kind) },
  { key: "last", label: "Last contact", get: (r) => [r.last] },
  {
    key: "channels",
    label: "Sources",
    get: (r) => r.channels,
  },
  {
    key: "linkedin",
    label: "LinkedIn",
    get: (r) => [r.public_identifier ? "Has LinkedIn" : "No LinkedIn"],
  },
  {
    key: "logbook",
    label: "Logbook",
    // No values until the saved logbooks are read: an unread catalog is not "No logbook".
    get: (r) => (r.logbook === null ? [] : [r.logbook ? "Has logbook" : "No logbook"]),
  },
  {
    key: "worth_source",
    label: "Decided by",
    get: (r) => one(r.worth_source),
    words: "source",
  },
  { key: "tags", label: "Your tags", get: (r) => r.tags },
  {
    key: "labels",
    label: "Relationship labels",
    get: (r) => r.labels,
    words: "labels",
  },
  { key: "function", label: "Function", get: (r) => one(r.function), words: "function" },
  { key: "warmth", label: "Warmth", get: (r) => one(r.warmthBucket) },
  { key: "direction", label: "Who writes", get: (r) => one(r.direction), words: "direction" },
  {
    key: "intro_source",
    label: "How you met",
    get: (r) => one(r.intro_source),
    words: "intro_source",
  },
  {
    key: "evidence",
    label: "Evidence",
    get: (r) => [
      ...(r.linkedin_only ? ["No relationship labels"] : []),
      ...(r.group_chat_only ? ["Group chats only"] : []),
      ...(r.shared_employer ? ["Shared employer"] : []),
      ...(r.shared_school ? ["Shared school"] : []),
    ],
  },
]

const FACET_BY_KEY: ReadonlyMap<string, FacetDef> = new Map(FACETS.map((facet) => [facet.key, facet]))

/** The Filters menu: each entry hides the rows its `hides` matches. Add a line here for a new one. */
export type HideKey = "linkedin_only"

export interface HideDef {
  key: HideKey
  label: string
  hides: (row: Person) => boolean
}

export const HIDES: readonly HideDef[] = [
  { key: "linkedin_only", label: "Hide LinkedIn-only people", hides: (r) => r.linkedin_only },
]

export function isHideKey(value: string): value is HideKey {
  return HIDES.some((entry) => entry.key === value)
}

export function isFacetKey(value: string): value is FacetKey {
  return FACET_BY_KEY.has(value)
}

/** The facet's definition: every FacetKey has one in FACETS. */
export function facetOf(key: FacetKey): FacetDef {
  return must(FACET_BY_KEY.get(key), `facet ${key}`)
}

// A choice the evidence could not settle reads N/A, not Unknown.
export function facetText(facet: FacetDef, value: string): string {
  if (value === "unknown") return "N/A"
  return facet.words ? label(facet.words, value) : sentence(value)
}

/** Held values by facet, as a view is set from them. */
export type FacetSet = Readonly<Partial<Record<FacetKey, readonly string[]>>>

/** A set's facets and values, in FACETS order. */
export function setEntries(set: FacetSet): [FacetKey, readonly string[]][] {
  return FACETS.flatMap(({ key }) => {
    const values = set[key]
    return values ? [[key, values]] : []
  })
}

export type SortKey = "name" | "relationship" | "worth" | "warmth" | "last" | "messages"

export interface Sort {
  key: SortKey
  dir: 1 | -1
}

const WORTH_ORDER = ["yes", "maybe", "no", ""]

const SORTERS: Readonly<Record<SortKey, (a: Person, b: Person) => number>> = {
  name: (a, b) => a.name.localeCompare(b.name),
  relationship: (a, b) => (a.relationship_kind || "~").localeCompare(b.relationship_kind || "~"),
  worth: (a, b) => WORTH_ORDER.indexOf(a.worth) - WORTH_ORDER.indexOf(b.worth),
  warmth: (a, b) => (b.warmth ?? -1) - (a.warmth ?? -1),
  last: (a, b) => (a.recency_days ?? 1e9) - (b.recency_days ?? 1e9),
  messages: (a, b) => b.interactions - a.interactions,
}

export function isSortKey(value: unknown): value is SortKey {
  return typeof value === "string" && Object.hasOwn(SORTERS, value)
}

export function sortRows(rows: readonly Person[], { key, dir }: Sort): Person[] {
  return [...rows].sort((a, b) => dir * SORTERS[key](a, b))
}

/** The human's worth call, written for a selection or the open person. */
export type WorthAction = "yes" | "no"
