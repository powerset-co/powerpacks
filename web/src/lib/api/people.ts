// The People page's routes on the Python review server (share/web/server.py).

import { body, failure } from "@/lib/api/http"
import { lastBucket, warmthBucket } from "@/lib/people/facets"
import {
  PERSON_COLUMNS,
  type NameResult,
  type PeoplePage,
  type PeoplePagePayload,
  type PeoplePayload,
  type Person,
  type PersonDetail,
  type RelationshipResult,
  type Worth,
  type WorthResult,
} from "@/types/people"

const API = "/api/people/"

/** Writes the human's relationship choice; returns the family's row as the server now holds it. */
export async function writeRelationship(
  parent_id: string,
  relationship_kind: string,
): Promise<RelationshipResult[]> {
  const response = await fetch(`${API}relationship`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ parent_id, relationship_kind }),
  })
  if (!response.ok) throw await failure(response, "Couldn't save relationship.")
  return (await body<{ rows: RelationshipResult[] }>(response)).rows
}

/** Writes the human's name for the family; returns the family's row as the server now holds it. */
export async function writeName(parent_id: string, name: string): Promise<NameResult[]> {
  const response = await fetch(`${API}name`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ parent_id, name }),
  })
  if (!response.ok) throw await failure(response, "Couldn't save the name.")
  return (await body<{ rows: NameResult[] }>(response)).rows
}

/** The text the search box matches a row on. */
export function searchText(row: Pick<Person, "name" | "title" | "company" | "location">): string {
  return `${row.name} ${row.title} ${row.company} ${row.location}`.toLowerCase()
}

type PersonCells = Omit<Person, "last" | "warmthBucket" | "search" | "logbook">

/** One `Person` per columnar row, decoded by column name, with the derived fields. Throws
 *  when the server stopped sending a column the page reads. */
export function decodePeople(payload: Pick<PeoplePayload, "columns" | "rows">): Person[] {
  const missing = PERSON_COLUMNS.filter((column) => !payload.columns.includes(column))
  if (missing.length) throw new Error(`People payload is missing columns: ${missing.join(", ")}`)
  return payload.rows.map((values) => {
    const cells = Object.fromEntries(payload.columns.map((column, position) => [column, values[position]]))
    // Cells are typed by their column name; the column list is checked above (LINT-WAIVERS.md W2).
    // eslint-disable-next-line @typescript-eslint/consistent-type-assertions -- W2
    const row = cells as PersonCells
    return {
      ...row,
      last: lastBucket(row.recency_days),
      warmthBucket: warmthBucket(row.warmth),
      search: searchText(row),
      logbook: null,
    }
  })
}

/** One slice of the view `query` (the page's own URL parameters, view.ts `viewQuery`). */
export async function fetchPeoplePage(
  query: string,
  offset: number,
  limit: number,
  signal?: AbortSignal,
): Promise<PeoplePage> {
  const response = await fetch(`${API}page?${query}&offset=${offset}&limit=${limit}`, { signal })
  if (!response.ok) throw await failure(response, "Couldn't load people.")
  const payload = await body<PeoplePagePayload>(response)
  const { yes = 0, no = 0, unjudged = 0 } = payload.totals
  return {
    matching: payload.matching,
    totals: { all: yes + no + unjudged, yes, no, unjudged },
    kinds: new Map(Object.entries(payload.kinds)),
    rows: decodePeople(payload),
  }
}

/** Every id the view `query` matches, in its order: select-all. */
export async function fetchPeopleIds(query: string): Promise<string[]> {
  const response = await fetch(`${API}page?${query}&ids=1`)
  if (!response.ok) throw await failure(response, "Couldn't select everyone.")
  return (await body<PeoplePagePayload>(response)).ids
}

export async function fetchPersonDetail(id: string, signal?: AbortSignal): Promise<PersonDetail> {
  const response = await fetch(`${API}person?id=${encodeURIComponent(id)}`, { signal })
  if (!response.ok) throw await failure(response, "Couldn't load details.")
  return body<PersonDetail>(response)
}

/** Deletes each parent's family from the store for good; returns how many were deleted. */
export async function deletePeople(ids: readonly string[]): Promise<number> {
  const response = await fetch(`${API}delete`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ids }),
  })
  if (!response.ok) throw await failure(response, "Try again.")
  return (await body<{ deleted: number }>(response)).deleted
}

/** Writes the human's worth call for each parent; returns their rows as the server now holds them. */
export async function writeWorth(ids: readonly string[], worth: Worth): Promise<WorthResult[]> {
  const response = await fetch(`${API}worth`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ids, worth }),
  })
  if (!response.ok) throw await failure(response, "Try again.")
  return (await body<{ rows: WorthResult[] }>(response)).rows
}
