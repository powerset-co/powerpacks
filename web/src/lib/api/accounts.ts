// The Accounts page's JSON routes on the Python review server (ingestion/primitives/accounts/api.py),
// and which sources are signed in, which setup's Connect screen shows.

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

/** Which sources need no sign-in, read where each one's sign-in lives (accounts.signed_in): the
 *  same answer setup takes. Gmail's mailboxes come with the Google app's owner first. */
export interface SignedIn {
  linkedin: boolean
  gmail: boolean
  imessage: boolean
  whatsapp: boolean
  mailboxes: string[]
}

export async function fetchSignedIn(signal?: AbortSignal): Promise<SignedIn> {
  const response = await fetch("/accounts/api/signed-in", { signal, cache: "no-store" })
  if (!response.ok) throw await failure(response, "Couldn't check your sign-ins.")
  return body<SignedIn>(response)
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

/** How far back a Gmail sync could go: what each window holds, what is synced, the measured pace. */
export interface GmailWindows {
  email: string
  windows: { years: number; messages: number }[]
  synced: number
  // Messages a second over the mailbox's completed syncs here.
  pace: number
}

export async function fetchGmailWindows(email: string, signal?: AbortSignal): Promise<GmailWindows> {
  const response = await fetch(`/accounts/api/gmail/windows?${new URLSearchParams({ email })}`, { signal })
  if (!response.ok) throw await failure(response, "Couldn't count that mailbox.")
  return body<GmailWindows>(response)
}

/** Syncs one mailbox over a window: `years` back, 0 for everything. */
export function syncGmailWindow(email: string, years: number): Promise<void> {
  return post(
    "/accounts/api/sync",
    { source: "gmail", email, years: String(years) },
    "Couldn't start the sync.",
  )
}
