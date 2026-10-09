// GET /api/account and POST /api/account/signin on the local server (packs/shared/web/account.py):
// the Powerset account this machine is signed in to, for the side nav's footer, and the sign-in
// the footer starts when nobody is.

import { body, failure } from "@/lib/api/http"

export interface Account {
  signed_in: boolean
  email: string | null
  /** The token's own name claim; null when it carries none. */
  name: string | null
}

/** Where the Powerset login's own callback server listens (packs/powerset/primitives/auth/auth.py):
 *  the desktop app's sign-in pane closes when it gets there. */

export async function fetchAccount(signal?: AbortSignal): Promise<Account> {
  const response = await fetch("/api/account", { signal, cache: "no-store" })
  if (!response.ok) throw await failure(response, "Couldn't read who is signed in.")
  return body<Account>(response)
}

/** Starts the Powerset sign-in on the server; its sign-in page's URL, for the desktop app's pane. */
export async function startSignIn(): Promise<string> {
  const response = await fetch("/api/account/signin", { method: "POST" })
  if (!response.ok) throw await failure(response, "Couldn't start the sign-in.")
  return (await body<{ url: string }>(response)).url
}
