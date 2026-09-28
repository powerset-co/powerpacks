// The Accounts page's one JSON route on the Python review server.

import { body, failure } from "@/lib/api/http"
import type { Account, AccountsPayload } from "@/types/accounts"

export async function fetchAccounts(signal?: AbortSignal): Promise<AccountsPayload> {
  const response = await fetch("/accounts/api/accounts", { signal })
  if (!response.ok) throw await failure(response, "Couldn't load accounts.")
  return body<AccountsPayload>(response)
}

async function post(path: string, form: Record<string, string>, fallback: string): Promise<void> {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams(form),
  })
  if (!response.ok) throw await failure(response, fallback)
}

/** Syncs one card now: a Gmail account by its address, or iMessage / WhatsApp. */
export function syncAccount(account: Account): Promise<void> {
  const form: Record<string, string> =
    account.source === "gmail" ? { source: "gmail", email: account.name } : { source: account.source }
  return post("/accounts/api/sync", form, "Couldn't start the sync.")
}

/** Opens Google's sign-in for one Gmail account; the server syncs Gmail once it lands. */
export function reconnectGmail(email: string): Promise<void> {
  return post("/accounts/api/reconnect", { email }, "Couldn't start the sign-in.")
}
