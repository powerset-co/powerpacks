// A search's tags (results.js normalizeTag, existingTag, toggleTag, removeTag): pure
// functions over Tagged, each returning a new value. The server keeps them (/searches/tags).

import { readStored } from "@/lib/storage"
import { isRecord } from "@/lib/utils"
import type { Tagged } from "@/types/searches"

export const TAG_NAME_MAX = 40
export const PIN_TAG = "Pinned"

export const NO_TAGS: Tagged = { tags: [], assignments: {} }

export function normalizeTag(value: string): string {
  return value.trim().slice(0, TAG_NAME_MAX)
}

/** The search's spelling of `tag`, matched case-insensitively. */
export function existingTag(tags: readonly string[], tag: string): string | undefined {
  const lower = tag.toLowerCase()
  return tags.find((existing) => existing.toLowerCase() === lower)
}

/** Whether the person holds the pin tag, in whatever spelling the search keeps it. */
export function isPinned(tagged: Tagged, personId: string): boolean {
  const pin = existingTag(tagged.tags, PIN_TAG)
  return pin !== undefined && (tagged.assignments[personId] ?? []).includes(pin)
}

function assign(assignments: Tagged["assignments"], personId: string, tags: string[]): Tagged["assignments"] {
  const others = Object.fromEntries(Object.entries(assignments).filter(([id]) => id !== personId))
  return tags.length ? { ...others, [personId]: tags } : others
}

/** Puts `tag` on the person, or takes it off; a new tag joins the search's list. */
export function toggleTag(tagged: Tagged, personId: string, raw: string): Tagged {
  const tag = normalizeTag(raw)
  if (!tag) return tagged
  const canonical = existingTag(tagged.tags, tag) ?? tag
  const tags = tagged.tags.includes(canonical) ? tagged.tags : [...tagged.tags, canonical]
  const held = tagged.assignments[personId] ?? []
  const next = held.includes(canonical) ? held.filter((value) => value !== canonical) : [...held, canonical]
  return { tags, assignments: assign(tagged.assignments, personId, next) }
}

/** Deletes `tag` from the search and from everyone holding it. */
export function removeTag(tagged: Tagged, raw: string): Tagged {
  const canonical = existingTag(tagged.tags, normalizeTag(raw))
  if (!canonical) return tagged
  let assignments = tagged.assignments
  for (const [personId, tags] of Object.entries(tagged.assignments)) {
    if (tags.includes(canonical)) {
      assignments = assign(
        assignments,
        personId,
        tags.filter((tag) => tag !== canonical),
      )
    }
  }
  return { tags: tagged.tags.filter((tag) => tag !== canonical), assignments }
}

/** Takes every tag off these people (results.js "Untag all on page"); the tag list stays. */
export function untagPeople(tagged: Tagged, personIds: readonly string[]): Tagged {
  let assignments = tagged.assignments
  for (const personId of personIds) assignments = assign(assignments, personId, [])
  return { tags: tagged.tags, assignments }
}

/** The tags `personIds` hold, first-seen order: the CSV filename's prefix. */
export function heldTags(tagged: Tagged, personIds: readonly string[]): string[] {
  return [...new Set(personIds.flatMap((personId) => tagged.assignments[personId] ?? []))]
}

// results.js kept a run's tags in this browser before the server did, and pins before tags.
const BROWSER_TAGS = "powerset_tagged_"
const BROWSER_PINS = "powerset_pinned_"

const isStrings = (raw: unknown): raw is string[] =>
  Array.isArray(raw) && raw.every((value) => typeof value === "string")

function parseTagged(raw: unknown): Tagged | null {
  if (!isRecord(raw) || !isStrings(raw.tags) || !isRecord(raw.assignments)) return null
  const assignments = Object.entries(raw.assignments).filter((entry): entry is [string, string[]] =>
    isStrings(entry[1]),
  )
  return { tags: raw.tags, assignments: Object.fromEntries(assignments) }
}

function parsePins(raw: unknown): Tagged | null {
  if (!Array.isArray(raw)) return null
  const ids = raw.filter((id): id is string => typeof id === "string")
  if (!ids.length) return null
  return { tags: [PIN_TAG], assignments: Object.fromEntries(ids.map((id) => [id, [PIN_TAG]])) }
}

/** results.js readTagged: the run's tags kept in this browser, else its old pins, else none. */
export function browserTags(runId: string): Tagged {
  return (
    readStored("local", `${BROWSER_TAGS}${runId}`, parseTagged) ??
    readStored("local", `${BROWSER_PINS}${runId}`, parsePins) ??
    NO_TAGS
  )
}
