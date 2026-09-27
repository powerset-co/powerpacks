// POST /searches/feedback on the Python review server (results_web/server.py _save_feedback).
// The hosted sign-in (/searches/auth/login) is not used: locally the server sends with the
// credentials `$powerset login` saved.

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
