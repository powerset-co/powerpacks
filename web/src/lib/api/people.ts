// The People page's four routes on the Python review server (share/web/server.py).

import { body, failure } from "@/lib/api/http"
import { lastBucket, warmthBucket } from "@/lib/people/facets"
import {
  PERSON_COLUMNS,
  type PeoplePayload,
  type Person,
  type PersonDetail,
  type TagChange,
  type TagResult,
} from "@/types/people"

const API = "/api/people/"

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

/** Posts the tags each parent should hold; returns the re-decided share rows. */
export async function writeTags(changes: TagChange[]): Promise<TagResult[]> {
  const response = await fetch(`${API}tags`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ people: changes }),
  })
  if (!response.ok) throw await failure(response, "Try again.")
  return (await body<{ rows: TagResult[] }>(response)).rows
}

export function avatarUrl(id: string): string {
  return `${API}avatar?id=${encodeURIComponent(id)}`
}
