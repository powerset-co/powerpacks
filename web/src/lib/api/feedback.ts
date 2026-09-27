// POST /searches/feedback on the Python review server (results_web/server.py _save_feedback).
// The hosted sign-in (/searches/auth/login) is not used: locally the server sends with the
// credentials `$powerset login` saved.

import { formValues, type FeedbackRecord, type FeedbackReply } from "@/lib/searches/feedback"

const API = "/searches/"

/** The server's message: a JSON `{error}` body, else the plain text, else the fallback copy. */
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

/**
 * The JSON body as the shape the server documents for the route (LINT-WAIVERS.md W1): the
 * Python handler on the same machine is the type.
 */
async function body<T>(response: Response): Promise<T> {
  // eslint-disable-next-line @typescript-eslint/consistent-type-assertions -- W1
  return (await response.json()) as T
}

export async function postFeedback(record: FeedbackRecord): Promise<FeedbackReply> {
  const response = await fetch(`${API}feedback`, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams(formValues(record)),
  })
  if (!response.ok) throw await failure(response, "Couldn't send feedback.")
  return body<FeedbackReply>(response)
}
