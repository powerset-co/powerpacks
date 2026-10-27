import { body, failure } from "./http"

export interface Provider {
  id: "parallel" | "modal" | "openai" | "typesafe" | "rapidapi-linkedin"
  name: string
  description: string
  connected: boolean
  credentials: { name: string; configured: boolean; masked: string | null }[]
  connect_url: string
}

export async function fetchProviders(signal: AbortSignal): Promise<{ providers: Provider[] }> {
  const response = await fetch("/api/settings/providers", { signal })
  if (!response.ok) throw await failure(response, "Couldn't read API connections.")
  return body<{ providers: Provider[] }>(response)
}

/** Writes typed keys to the checkout's .env; answers the rows as read afterwards. */
export async function saveProviderKeys(values: Record<string, string>): Promise<{ providers: Provider[] }> {
  const response = await fetch("/api/settings/providers", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ values }),
  })
  if (!response.ok) throw await failure(response, "Couldn't save the key.")
  return body<{ providers: Provider[] }>(response)
}
