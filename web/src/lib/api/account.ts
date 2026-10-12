import { body, failure } from "@/lib/api/http"

export interface Account {
  signed_in: boolean
  email: string | null
  /** The token's own name claim; null when it carries none. */
  name: string | null
}

export async function fetchAccount(signal?: AbortSignal): Promise<Account> {
  const response = await fetch("/api/account", { signal, cache: "no-store" })
  if (!response.ok) throw await failure(response, "Couldn't read who is signed in.")
  return body<Account>(response)
}
