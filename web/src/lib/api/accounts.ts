// The Accounts page's JSON routes on the Python review server (ingestion/primitives/accounts/api.py),
// and which sources are signed in, which setup's Connect screen shows.

import { body, failure } from "@/lib/api/http"
import type { AccountsPayload } from "@/types/accounts"

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

/** `sources` reads only those: asking for iMessage alone never runs msgvault's Gmail check,
 *  which takes seconds on a large mailbox. */
export async function fetchSignedIn(
  signal?: AbortSignal,
  sources?: (keyof Omit<SignedIn, "mailboxes">)[],
): Promise<SignedIn> {
  const query = sources ? `?sources=${sources.join(",")}` : ""
  const response = await fetch(`/accounts/api/signed-in${query}`, { signal, cache: "no-store" })
  if (!response.ok) throw await failure(response, "Couldn't check your sign-ins.")
  return body<SignedIn>(response)
}

/** One of the user's Chrome or Brave profiles holding a LinkedIn or Google session (chrome_profiles.py). */
export interface ChromeProfile {
  browser: "chrome" | "brave"
  folder: string
  name: string
  email: string
  linkedin: boolean
  google: boolean
}

/** The profiles to pick from, and the one the sign-in browser was copied from (null before a copy). */
export interface ChromeProfiles {
  profiles: ChromeProfile[]
  linked: Pick<ChromeProfile, "browser" | "folder"> | null
}

export async function fetchChromeProfiles(signal?: AbortSignal): Promise<ChromeProfiles> {
  const response = await fetch("/accounts/api/chrome-profiles", { signal })
  if (!response.ok) throw await failure(response, "Couldn't read your Chrome profiles.")
  return body<ChromeProfiles>(response)
}

/** Copies one profile's sign-ins and saved passwords into the browser Powerpacks signs in with. */
export function copyChromeProfile({ browser, folder }: Pick<ChromeProfile, "browser" | "folder">): Promise<void> {
  return post("/accounts/api/chrome-profile", { browser, folder }, "Couldn't copy that Chrome profile.")
}

/** Opens Google's sign-in for one Gmail account; the server syncs Gmail once it lands. */
export function reconnectGmail(email: string): Promise<void> {
  return post("/accounts/api/reconnect", { email }, "Couldn't start the sign-in.")
}
