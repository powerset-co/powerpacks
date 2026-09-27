// A search's tags (results.js normalizeTag, existingTag, toggleTag, removeTag): pure
// functions over Tagged, each returning a new value. The server keeps them (/searches/tags).

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
