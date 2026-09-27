// The one place responses become values: the review server's error shape and its JSON bodies.

import { isRecord } from "@/lib/utils"

/** The server's message: a JSON `{error}` body, else the plain text, else the page's fallback copy. */
export async function failure(response: Response, fallback: string): Promise<Error> {
  const text = await response.text()
  try {
    const body: unknown = JSON.parse(text)
    if (isRecord(body) && typeof body.error === "string") return new Error(body.error)
  } catch {
    // Not JSON: the text is the message.
  }
  return new Error(text || fallback)
}

/** A caught value's words: an Error's message, else the value as text. */
export function errorText(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

/**
 * The JSON body as the shape the server documents for the route. The app's one type
 * assertion (LINT-WAIVERS.md W1): the Python dataclasses on the same machine are the type,
 * pinned to types/ by tests/test_share_web.py and tests/test_search_json_contract.py.
 */
export async function body<T>(response: Response): Promise<T> {
  // eslint-disable-next-line @typescript-eslint/consistent-type-assertions -- W1
  return (await response.json()) as T
}
