// The People page's sets routes (share/web/sets.py): the sets the owner belongs to, kept locally,
// refreshed from the cloud on request; create and delete go through the cloud.

import { body, failure } from "@/lib/api/http"
import { isRecord } from "@/lib/utils"

export interface SetMember {
  name: string
  email: string
  role: string
}

export interface SetView {
  set_id: string
  name: string
  role: string
  is_personal: boolean
  member_count: number
  person_count: number
  members: SetMember[]
  refreshed_at: string
}

export interface SetsPayload {
  sets: SetView[]
  /** How many people the owner shares: the share list's yes rows. */
  shared: number
  default_set_id: string
}

const URL = "/api/people/sets"
const JSON_BODY = { "Content-Type": "application/json" }
const NEEDS_AUTH = "needs_auth"

/** A refusal: `needsAuth` when Powerset has no sign-in on this machine. */
export class SetsError extends Error {
  readonly needsAuth: boolean
  constructor(message: string, needsAuth: boolean) {
    super(message)
    this.name = "SetsError"
    this.needsAuth = needsAuth
  }
}

async function answer(response: Response): Promise<SetsPayload> {
  if (response.ok) return body<SetsPayload>(response)
  let needsAuth = false
  try {
    const payload: unknown = await response.clone().json()
    needsAuth = isRecord(payload) && payload.status === NEEDS_AUTH
  } catch {
    // Not JSON: the text is the message.
  }
  const { message } = await failure(response, "Couldn't read your sets.")
  throw new SetsError(message, needsAuth)
}

export async function fetchSets(refresh: boolean): Promise<SetsPayload> {
  return answer(await fetch(refresh ? `${URL}?refresh=1` : URL, { cache: "no-store" }))
}

export async function createSet(name: string): Promise<SetsPayload> {
  return answer(await fetch(URL, { method: "POST", headers: JSON_BODY, body: JSON.stringify({ name }) }))
}

export async function deleteSet(set_id: string): Promise<SetsPayload> {
  return answer(
    await fetch(`${URL}/delete`, { method: "POST", headers: JSON_BODY, body: JSON.stringify({ set_id }) }),
  )
}
