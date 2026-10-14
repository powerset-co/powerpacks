// The People page's sets routes (share/web/sets.py): sets live on this machine, never in the cloud.
// Invites ride the relay: sent to an email, answered with Accept or Decline; an accepted invite is a set
// joined on this machine. A set's people are its members' shared people in the share_v1 network.

import { body, failure } from "@/lib/api/http"
import { isRecord } from "@/lib/utils"

export interface SetMember {
  name: string
  email: string
  role: string
  operator_id: string
  /** The member's last relay heartbeat; "" when this machine has never seen one. */
  last_seen_at: string
  /** People this member shares into the share_v1 network: what they bring to the set. */
  person_count: number
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
}

export interface SetsPayload {
  sets: SetView[]
  invites: ReceivedInvite[]
  /** How many people the owner shares: the share list's yes rows. */
  shared: number
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

/** The refusal a sets route (or an ask route, which asks the sets) answered with. */
export async function refusal(response: Response, fallback: string): Promise<SetsError> {
  let needsAuth = false
  try {
    const payload: unknown = await response.clone().json()
    needsAuth = isRecord(payload) && payload.status === NEEDS_AUTH
  } catch {
    // Not JSON: the text is the message.
  }
  const { message } = await failure(response, fallback)
  return new SetsError(message, needsAuth)
}

async function answer(response: Response): Promise<SetsPayload> {
  if (response.ok) return body<SetsPayload>(response)
  throw await refusal(response, "Couldn't read your sets.")
}

export async function fetchSets(): Promise<SetsPayload> {
  return answer(await fetch(URL, { cache: "no-store" }))
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
