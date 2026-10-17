// The Powerset team: who is on the relay and when, and the support thread with one teammate's agent.
// Served by the local page server, which talks to the relay (packs/shared/web/team.py).

import { body, failure } from "./http"

const URL = "/api/team"
const JSON_BODY = { "Content-Type": "application/json" }

export interface Teammate {
  operator_id: string
  name: string
  email: string
  /** Their agent's last inbox poll as the relay saw it; "" when it never polled. */
  last_seen_at: string
}

export interface TeamPayload {
  me: { email: string }
  members: Teammate[]
}

/** One line of a support thread: what we asked, or what their agent answered. */
export interface SupportMessage {
  id: string
  direction: "out" | "in"
  text: string
  at: string
}

export async function fetchTeam(): Promise<TeamPayload> {
  const response = await fetch(URL, { cache: "no-store" })
  if (!response.ok) throw await failure(response, "Couldn't read the team.")
  return body<TeamPayload>(response)
}

export async function fetchThread(operatorId: string): Promise<SupportMessage[]> {
  const response = await fetch(`${URL}/thread?operator_id=${encodeURIComponent(operatorId)}`, {
    cache: "no-store",
  })
  if (!response.ok) throw await failure(response, "Couldn't read the thread.")
  return (await body<{ messages: SupportMessage[] }>(response)).messages
}

/** Sends a support request to the teammate's agent; returns the thread with it appended. */
export async function askAgent(operatorId: string, text: string): Promise<SupportMessage[]> {
  const response = await fetch(`${URL}/ask`, {
    method: "POST",
    headers: JSON_BODY,
    body: JSON.stringify({ operator_id: operatorId, text }),
  })
  if (!response.ok) throw await failure(response, "Couldn't send that.")
  return (await body<{ messages: SupportMessage[] }>(response)).messages
}
