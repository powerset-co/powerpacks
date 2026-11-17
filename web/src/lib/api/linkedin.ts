// The search drawer's Get Mutuals: one person's mutual connections through this Mac's LinkedIn
// session (linkedin_session.mutuals behind POST /api/linkedin/mutuals).

import { body, failure } from "@/lib/api/http"

export interface MutualPerson {
  name: string
  headline: string
  location: string
  url: string
}

/** linkedin_session's row: connected (1st degree), cached or looked_up (count + first ten), over_budget, ... */
export interface MutualsAnswer {
  status: "no_slug" | "connected" | "cached" | "looked_up" | "over_budget" | "not_answered"
  count?: number | null
  people?: MutualPerson[]
  error?: string
  requests_today: number
  day_cap: number
}

export async function fetchMutuals(url: string): Promise<MutualsAnswer> {
  const response = await fetch("/api/linkedin/mutuals", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ url }),
  })
  if (!response.ok) throw await failure(response, "Couldn't get mutual connections.")
  return body<MutualsAnswer>(response)
}
