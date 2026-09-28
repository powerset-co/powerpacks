// Facets, quick filters, the filter/count pass, sorting and the tag toggle, ported 1:1 from people.js.
// Facets: OR within one, AND across, always within the selected decision tab.

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
  | "worth_source"
  | "tags"
  | "labels"
  | "function"
  | "seniority"
  | "mode"
  | "warmth"
  | "direction"
  | "hierarchy"
  | "intro_source"
  | "evidence"

/** The values held per facet: the view's filters. */
export type FacetFilters = ReadonlyMap<FacetKey, ReadonlySet<string>>

/** `get` returns the row's values for the facet; `words` names the TEXT map that words its values. */
export interface FacetDef {
  key: FacetKey
  label: string
  get: (row: Person) => string[]
  order?: readonly string[]
  words?: TextKind
  more?: boolean
  search?: boolean
}

const one = (value: string): string[] => (value ? [value] : [])

// The default facets stay open; the `more` ones collapse under "More filters".
export const FACETS: readonly FacetDef[] = [
  { key: "reason", label: "Reason", get: (r) => [r.reason], words: "reason" },
  {
    key: "worth",
    label: "Worth",
    get: (r) => [r.worth || "unjudged"],
    order: ["yes", "maybe", "no", "unjudged"],
    words: "worth",
  },
  { key: "relationship_kind", label: "Relationship", get: (r) => one(r.relationship_kind) },
  { key: "last", label: "Last contact", get: (r) => [r.last], order: LAST },
  {
    key: "channels",
    label: "Sources",
    get: (r) => r.channels,
    order: ["gmail", "imessage", "whatsapp", "linkedin"],
  },
  {
    key: "linkedin",
    label: "LinkedIn",
    get: (r) => [r.public_identifier ? "Has LinkedIn" : "No LinkedIn"],
    order: ["Has LinkedIn", "No LinkedIn"],
  },
  {
    key: "worth_source",
    label: "Worth decided by",
    get: (r) => one(r.worth_source),
    words: "source",
    more: true,
  },
  { key: "tags", label: "Your tags", get: (r) => r.tags, more: true },
  {
    key: "labels",
    label: "Relationship labels",
    get: (r) => r.labels,
    words: "labels",
    more: true,
    search: true,
  },
  { key: "function", label: "Function", get: (r) => one(r.function), words: "function", more: true },
  { key: "seniority", label: "Seniority", get: (r) => one(r.seniority), words: "seniority", more: true },
  { key: "mode", label: "Conversation", get: (r) => one(r.mode), words: "mode", more: true },
  { key: "warmth", label: "Warmth", get: (r) => one(r.warmthBucket), order: WARMTH, more: true },
  { key: "direction", label: "Who writes", get: (r) => one(r.direction), words: "direction", more: true },
  {
    key: "hierarchy",
    label: "Reporting relationship",
    get: (r) => one(r.hierarchy),
    words: "hierarchy",
    more: true,
  },
  {
    key: "intro_source",
    label: "How you met",
    get: (r) => one(r.intro_source),
    words: "intro_source",
    more: true,
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
    more: true,
  },
]

const FACET_BY_KEY: ReadonlyMap<string, FacetDef> = new Map(FACETS.map((facet) => [facet.key, facet]))

export function isFacetKey(value: string): value is FacetKey {
  return FACET_BY_KEY.has(value)
}

/** The facet's definition: every FacetKey has one in FACETS. */
export function facetOf(key: FacetKey): FacetDef {
  return must(FACET_BY_KEY.get(key), `facet ${key}`)
}

export function facetText(facet: FacetDef, value: string): string {
  return facet.words ? label(facet.words, value) : sentence(value)
}

/** Held values by facet, as a quick filter names them and a view is set from them. */
export type FacetSet = Readonly<Partial<Record<FacetKey, readonly string[]>>>

/** A set's facets and values, in FACETS order. */
export function setEntries(set: FacetSet): [FacetKey, readonly string[]][] {
  return FACETS.flatMap(({ key }) => {
    const values = set[key]
    return values ? [[key, values]] : []
  })
}

/** Quick filters: named facet selections, counted within the tab. */
export interface QuickFilter {
  name: string
  set: FacetSet
}

export const QUICK: readonly QuickFilter[] = [
  { name: "Family", set: { relationship_kind: ["family"] } },
  { name: "Sensitive context", set: { labels: ["sensitive_context"] } },
  { name: "Service providers", set: { relationship_kind: ["service_provider"] } },
  { name: "Recruiters", set: { labels: ["is_recruiter"] } },
  { name: "Strangers", set: { labels: ["is_stranger"] } },
  { name: "Automated senders", set: { labels: ["is_automated_sender"] } },
  { name: "Last contact > 2 years", set: { last: [LAST[2]] } },
  { name: "Close friends", set: { relationship_kind: ["close_friend"] } },
]

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

/** A quick filter is on when the held facets are exactly its set, nothing more. */
export function quickActive(quick: QuickFilter, filters: FacetFilters): boolean {
  const entries = setEntries(quick.set)
  const heldKeys = [...filters].filter(([, values]) => values.size).length
  return (
    heldKeys === entries.length &&
    entries.every(([key, values]) => {
      const held = filters.get(key)
      return held?.size === values.length && values.every((value) => held.has(value))
    })
  )
}

/** A facet's values for the rail: its fixed order when it has one, else most people first; held
 *  values stay listed at zero. */
export function orderedValues(
  facet: FacetDef,
  counts: ReadonlyMap<string, number>,
  held: ReadonlySet<string>,
): string[] {
  const values = [...new Set([...counts.keys(), ...held])]
  const { order } = facet
  if (order) return values.sort((a, b) => (order.indexOf(a) + 1 || 99) - (order.indexOf(b) + 1 || 99))
  return values.sort((a, b) => (counts.get(b) ?? 0) - (counts.get(a) ?? 0) || a.localeCompare(b))
}

export function sortRows(rows: readonly Person[], { key, dir }: Sort): Person[] {
  return [...rows].sort((a, b) => dir * SORTERS[key](a, b))
}

export type TagAction = "share" | "private"

/** The tags a person holds after a share / keep-private action. */
export function nextTags(row: Person, action: TagAction): string[] {
  const tags = new Set(row.tags)
  if (action === "share") {
    tags.delete("private")
    tags.add("share")
  }
  if (action === "private") {
    tags.delete("share")
    tags.add("private")
  }
  return [...tags].sort()
}
