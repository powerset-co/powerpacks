import { body, failure } from "./http"

export interface Provider {
  id: string
  name: string
  description: string
  connected: boolean
  credentials: { name: string; configured: boolean; masked: string | null }[]
  connect_url: string
  dashboard_url: string
}

export async function fetchProviders(signal: AbortSignal): Promise<{ providers: Provider[] }> {
  const response = await fetch("/api/settings/providers", { signal })
  if (!response.ok) throw await failure(response, "Couldn't read API connections.")
  return body<{ providers: Provider[] }>(response)
}
