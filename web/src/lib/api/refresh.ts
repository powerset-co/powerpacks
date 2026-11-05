// The native daily refresh's JSON routes on the Python server (packs/ingestion/primitives/refresh/api.py).

import { body, failure } from "@/lib/api/http"
import type { RefreshSettingsChange, RefreshState } from "@/types/refresh"

export const REFRESH_KEY = ["refresh"]

export async function fetchRefresh(signal?: AbortSignal): Promise<RefreshState> {
  const response = await fetch("/tasks/api/refresh", { signal })
  if (!response.ok) throw await failure(response, "Couldn't load the refresh.")
  return body<RefreshState>(response)
}

/** Turn the refresh on or off, or change how often, which day or what time; resolves to the state
 *  as it now stands. */
export async function saveRefreshSettings(settings: RefreshSettingsChange): Promise<RefreshState> {
  const fields: Record<string, string> = {}
  for (const [name, value] of Object.entries(settings)) fields[name] = String(value)
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
