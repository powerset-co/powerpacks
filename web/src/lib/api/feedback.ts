// POST /searches/feedback on the Python review server (results_web/server.py _save_feedback),
// POST /api/feedback for the People drawer's flag (shared/web/feedback.py), and POST
// /searches/auth/login, which runs `$powerset login` on this machine when Powerset answers that
// it needs a sign-in. In the desktop app the login's page shows in the app's sign-in pane
// instead: /auth/login/start hands over its URL, /auth/login/finish its outcome.

import { body, failure } from "@/lib/api/http"
import { isDesktop } from "@/lib/desktop"
import { formValues } from "@/lib/searches/feedback"
import { openSignIn, POWERSET_CALLBACK, signInFinished } from "@/lib/signin"
import type { Person } from "@/types/people"
import type { FeedbackRecord, FeedbackReply } from "@/types/searches"

const LOGIN = "/searches/auth/login"
const NOT_SIGNED_IN = "Sign-in did not complete. Try again."

export async function postFeedback(record: FeedbackRecord): Promise<FeedbackReply> {
  const response = await fetch("/searches/feedback", {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams(formValues(record)),
  })
  if (!response.ok) throw await failure(response, "Couldn't send feedback.")
  return body<FeedbackReply>(response)
}

/** A note about one person for the Powerset team, with who they are and the worth and share call
 *  the drawer showed; throws why it did not go out. */
export async function sendPersonFeedback(comment: string, row: Person): Promise<void> {
  const person = {
    parent_id: row.parent_id,
    name: row.name,
    public_identifier: row.public_identifier,
    title: row.title,
    company: row.company,
    worth: row.worth,
    worth_source: row.worth_source,
    share: row.share,
    share_source: row.share_source,
    reason: row.reason,
    labels: row.labels,
    confidence: row.confidence,
  }
  const response = await fetch("/api/feedback", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ comment, person }),
  })
  if (!response.ok) throw await failure(response, "Couldn't send feedback. Try again.")
}

/** Runs the Powerset sign-in on the server; throws the server's message when it did not complete.
 *  In the desktop app the page shows in the sign-in pane, never in a browser. */
export async function signIn(): Promise<void> {
  if (isDesktop()) return signInInApp()
  const response = await fetch(LOGIN, { method: "POST" })
  if (response.status === 409) location.assign("/install")
  if (!response.ok) throw await failure(response, NOT_SIGNED_IN)
}

async function signInInApp(): Promise<void> {
  const started = await fetch(`${LOGIN}/start`, { method: "POST" })
  if (started.status === 409) location.assign("/install")
  if (!started.ok) throw await failure(started, NOT_SIGNED_IN)
  const { url } = await body<{ url: string }>(started)
  openSignIn({ title: "Powerset", url, finish: POWERSET_CALLBACK })
  if (!(await signInFinished())) throw new Error("The sign-in was closed before it finished.")
  const finished = await fetch(`${LOGIN}/finish`, { method: "POST" })
  if (!finished.ok) throw await failure(finished, NOT_SIGNED_IN)
}
