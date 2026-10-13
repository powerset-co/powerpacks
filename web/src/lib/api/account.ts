import { body, failure } from "@/lib/api/http"

export interface Account {
  signed_in: boolean
  email: string | null
  /** The name and picture the login stored (auth.py); null when it stored none. */
  name: string | null
  picture: string | null
}

export async function fetchAccount(signal?: AbortSignal): Promise<Account> {
  const response = await fetch("/api/account", { signal, cache: "no-store" })
  if (!response.ok) throw await failure(response, "Couldn't read who is signed in.")
  return body<Account>(response)
}
