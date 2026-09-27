// POST /searches/feedback on the Python review server (results_web/server.py _save_feedback),
// and POST /searches/auth/login, which runs `$powerset login` on this machine when Powerset
// answers that it needs a sign-in.

import { body, failure } from "@/lib/api/http"
import { formValues } from "@/lib/searches/feedback"
import type { FeedbackRecord, FeedbackReply } from "@/types/searches"

export async function postFeedback(record: FeedbackRecord): Promise<FeedbackReply> {
  const response = await fetch("/searches/feedback", {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams(formValues(record)),
  })
  if (!response.ok) throw await failure(response, "Couldn't send feedback.")
  return body<FeedbackReply>(response)
}

/** Runs the Powerset sign-in on the server; throws the server's message when it did not complete. */
export async function signIn(): Promise<void> {
  const response = await fetch("/searches/auth/login", { method: "POST" })
  if (!response.ok) throw await failure(response, "Sign-in did not complete. Try again.")
}
