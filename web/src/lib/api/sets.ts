// The People page's sets routes (share/web/sets.py): the sets the owner belongs to, kept locally,
// refreshed from the cloud on request; create and delete go through the cloud. Invites ride the relay:
// sent to an email, answered with Accept or Decline; an accepted invite is a set joined on this machine.

import { body, failure } from "@/lib/api/http"
import { isRecord } from "@/lib/utils"

export interface SetMember {
  name: string
  email: string
  role: string
  operator_id: string
  /** The member's last relay heartbeat; "" when this machine has never seen one. */
  last_seen_at: string
}

/** An invite this owner sent that has no accepted answer yet. */
export interface SentInvite {
  id: string
  email: string
  status: "pending" | "declined"
}

/** An invite to this owner, waiting for Accept or Decline. */
export interface ReceivedInvite {
  id: string
  set_name: string
  from: string
  /** The inviter's email, carried in the invite; "" on an invite from an older build. */
  from_email: string
  created_at: string
}

export interface SetView {
  set_id: string
  name: string
  role: string
  is_personal: boolean
  member_count: number
  person_count: number
  members: SetMember[]
  invited: SentInvite[]
  refreshed_at: string
}

export interface SetsPayload {
  sets: SetView[]
  invites: ReceivedInvite[]
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

export async function inviteToSet(set_id: string, email: string): Promise<SetsPayload> {
  return answer(
    await fetch(`${URL}/invite`, {
      method: "POST",
      headers: JSON_BODY,
      body: JSON.stringify({ set_id, email }),
    }),
  )
}

export async function answerInvite(invite_id: string, accepted: boolean): Promise<SetsPayload> {
  return answer(
    await fetch(`${URL}/answer`, {
      method: "POST",
      headers: JSON_BODY,
      body: JSON.stringify({ invite_id, accepted }),
    }),
  )
}
