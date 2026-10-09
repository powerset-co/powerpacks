// POST /searches/feedback on the Python review server (results_web/server.py _save_feedback),
// and POST /searches/auth/login, which runs `$powerset login` on this machine when Powerset
// answers that it needs a sign-in. In the desktop app the login's page shows in the app's
// sign-in pane instead: /auth/login/start hands over its URL, /auth/login/finish its outcome.

import { body, failure } from "@/lib/api/http"
import { isDesktop } from "@/lib/desktop"
import { formValues } from "@/lib/searches/feedback"
import { openSignIn, POWERSET_CALLBACK, signInFinished } from "@/lib/signin"
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

/** Runs the Powerset sign-in on the server; throws the server's message when it did not complete.
 *  In the desktop app the page shows in the sign-in pane, never in a browser. */
export async function signIn(): Promise<void> {
  if (isDesktop()) return signInInApp()
  const response = await fetch(LOGIN, { method: "POST" })
  if (!response.ok) throw await failure(response, NOT_SIGNED_IN)
}

async function signInInApp(): Promise<void> {
  const started = await fetch(`${LOGIN}/start`, { method: "POST" })
  if (!started.ok) throw await failure(started, NOT_SIGNED_IN)
  const { url } = await body<{ url: string }>(started)
  openSignIn({ title: "Powerset", url, finish: POWERSET_CALLBACK })
  if (!(await signInFinished())) throw new Error("The sign-in was closed before it finished.")
  const finished = await fetch(`${LOGIN}/finish`, { method: "POST" })
  if (!finished.ok) throw await failure(finished, NOT_SIGNED_IN)
}
