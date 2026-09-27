// The Searches page's JSON routes and saved tags on the Python review server.

import type { CatalogPayload, SearchRunPayload, Tagged, TagsPayload } from "@/types/searches"

const API = "/searches"

/** The server's JSON error, plain text, or fallback copy. */
async function failure(response: Response, fallback: string): Promise<Error> {
  const text = await response.text()
  try {
    const body: unknown = JSON.parse(text)
    if (body && typeof body === "object" && "error" in body && typeof body.error === "string") {
      return new Error(body.error)
    }
  } catch {
    // Not JSON: the text is the message.
  }
  return new Error(text || fallback)
}

/** The Python dataclass JSON contract, pinned by tests/test_search_json_contract.py. */
async function body<T>(response: Response): Promise<T> {
  // eslint-disable-next-line @typescript-eslint/consistent-type-assertions -- W1
  return (await response.json()) as T
}

export async function fetchCatalog(signal?: AbortSignal): Promise<CatalogPayload> {
  const response = await fetch(`${API}/api/catalog`, { signal })
  if (!response.ok) throw await failure(response, "Couldn't load searches.")
  return body<CatalogPayload>(response)
}

export async function fetchSearchRun(runId: string, signal?: AbortSignal): Promise<SearchRunPayload> {
  const response = await fetch(`${API}/api/search.json?run_id=${encodeURIComponent(runId)}`, { signal })
  if (!response.ok) throw await failure(response, "Couldn't load search.")
  return body<SearchRunPayload>(response)
}

export async function fetchTags(runId: string): Promise<TagsPayload> {
  const response = await fetch(`${API}/tags?run_id=${encodeURIComponent(runId)}`)
  if (!response.ok) throw await failure(response, "Couldn't load tags.")
  return body<TagsPayload>(response)
}

export async function writeTags(runId: string, tagged: Tagged): Promise<{ ok: boolean }> {
  const response = await fetch(`${API}/tags`, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({ run_id: runId, person_id: "", comment: "", tagged: JSON.stringify(tagged) }),
  })
  if (!response.ok) throw await failure(response, "Couldn't save tags.")
  return body<{ ok: boolean }>(response)
}
