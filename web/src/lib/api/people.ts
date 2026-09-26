// The People page's four routes on the Python review server (share/web/server.py).

import { lastBucket, warmthBucket } from "@/lib/people/facets";
import type { PeoplePayload, Person, PersonDetail, TagChange, TagResult } from "@/types/people";

const API = "/api/people/";

/** The server's message: a JSON `{error}` body, else the plain text, else the page's fallback copy. */
async function failure(response: Response, fallback: string): Promise<Error> {
  const text = await response.text();
  try {
    const body: unknown = JSON.parse(text);
    if (body && typeof body === "object" && "error" in body && typeof body.error === "string") {
      return new Error(body.error);
    }
  } catch {
    // Not JSON: the text is the message.
  }
  return new Error(text || fallback);
}

/** One `Person` per columnar row, decoded by column name, with the derived fields. */
export function decodePeople(payload: PeoplePayload): Person[] {
  return payload.rows.map((values) => {
    const cells = Object.fromEntries(payload.columns.map((column, position) => [column, values[position]]));
    const row = cells as Omit<Person, "last" | "warmthBucket" | "search">;
    return {
      ...row,
      last: lastBucket(row.recency_days),
      warmthBucket: warmthBucket(row.warmth),
      search: `${row.name} ${row.title} ${row.company} ${row.location}`.toLowerCase(),
    };
  });
}

export async function fetchPeople(): Promise<Person[]> {
  const response = await fetch(`${API}rows`);
  if (!response.ok) throw await failure(response, "Couldn't load people.");
  return decodePeople((await response.json()) as PeoplePayload);
}

export async function fetchPersonDetail(id: string, signal?: AbortSignal): Promise<PersonDetail> {
  const response = await fetch(`${API}person?id=${encodeURIComponent(id)}`, { signal });
  if (!response.ok) throw await failure(response, "Couldn't load details.");
  return (await response.json()) as PersonDetail;
}

/** Posts the tags each parent should hold; returns the re-decided share rows. */
export async function writeTags(changes: TagChange[]): Promise<TagResult[]> {
  const response = await fetch(`${API}tags`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ people: changes }),
  });
  if (!response.ok) throw await failure(response, "Try again.");
  return ((await response.json()) as { rows: TagResult[] }).rows;
}

export function avatarUrl(id: string): string {
  return `${API}avatar?id=${encodeURIComponent(id)}`;
}
