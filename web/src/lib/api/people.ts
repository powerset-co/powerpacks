// The People page's four routes on the Python review server (share/web/server.py).

import { body, failure } from "@/lib/api/http"
import { lastBucket, warmthBucket } from "@/lib/people/facets"
import {
  PERSON_COLUMNS,
  type PeoplePayload,
  type Person,
  type PersonDetail,
  type Worth,
  type WorthResult,
} from "@/types/people"

const API = "/api/people/"

export async function writeRelationship(parent_id: string, relationship_kind: string): Promise<void> {
  const response = await fetch(`${API}relationship`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ parent_id, relationship_kind }),
  })
  if (!response.ok) throw await failure(response, "Couldn't save relationship.")
}

type PersonCells = Omit<Person, "last" | "warmthBucket" | "search" | "logbook">

/** One `Person` per columnar row, decoded by column name, with the derived fields. Throws
 *  when the server stopped sending a column the page reads. */
export function decodePeople(payload: PeoplePayload): Person[] {
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
      search: `${row.name} ${row.title} ${row.company} ${row.location}`.toLowerCase(),
      logbook: null,
    }
  })
}

export async function fetchPeople(): Promise<Person[]> {
  const response = await fetch(`${API}rows`)
  if (!response.ok) throw await failure(response, "Couldn't load people.")
  return decodePeople(await body<PeoplePayload>(response))
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
