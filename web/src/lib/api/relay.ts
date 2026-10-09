// GET /api/relay and POST /api/relay/connect on the local server (packs/shared/web/server.py):
// the Ask the Set daemon's link to the Powerset relay, for the top bar's status dot.

import { body, failure } from "@/lib/api/http"

export interface RelayStatus {
  state: "connected" | "signed_out" | "offline"
}

export async function fetchRelay(signal?: AbortSignal): Promise<RelayStatus> {
  const response = await fetch("/api/relay", { signal })
  if (!response.ok) throw await failure(response, "Couldn't read the relay status.")
  return body<RelayStatus>(response)
}

/** Wakes a signed-out daemon so it reconnects now rather than at its next retry. */
export async function connectRelay(): Promise<RelayStatus> {
  const response = await fetch("/api/relay/connect", { method: "POST" })
  if (!response.ok) throw await failure(response, "Couldn't reach the relay.")
  return body<RelayStatus>(response)
}
