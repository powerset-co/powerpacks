// The native daily refresh's JSON routes on the Python server (packs/ingestion/primitives/refresh/api.py).

import { body, failure } from "@/lib/api/http"
import type { RefreshState } from "@/types/refresh"

export const REFRESH_KEY = ["refresh"]

export async function fetchRefresh(signal?: AbortSignal): Promise<RefreshState> {
  const response = await fetch("/tasks/api/refresh", { signal })
  if (!response.ok) throw await failure(response, "Couldn't load the refresh.")
  return body<RefreshState>(response)
}

/** Turn the daily refresh on or off, or move its time; resolves to the state as it now stands. */
export async function saveRefreshSettings(settings: {
  enabled?: boolean
  time?: string
}): Promise<RefreshState> {
  const fields: Record<string, string> = {}
  if (settings.enabled !== undefined) fields.enabled = String(settings.enabled)
  if (settings.time !== undefined) fields.time = settings.time
  const response = await fetch("/tasks/api/refresh/settings", {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams(fields),
  })
  if (!response.ok) throw await failure(response, "Couldn't change the refresh.")
  return body<RefreshState>(response)
}

/** Start a refresh now (the running one, when there is one). */
export async function refreshNow(): Promise<RefreshState> {
  const response = await fetch("/tasks/api/refresh/run", { method: "POST" })
  if (!response.ok) throw await failure(response, "Couldn't start the refresh.")
  return body<RefreshState>(response)
}
