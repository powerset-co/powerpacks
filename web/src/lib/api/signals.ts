import { body, failure } from "@/lib/api/http"

export interface SignalsPlan {
  people: number
  cached: number
  new_requests: number
  estimated_cost_usd: number
  checked: string
}

export interface SignalsStatus {
  status: "idle" | "running" | "completed" | "failed"
  message: string | null
  plan: SignalsPlan | null
  error: string | null
}

export async function readSignals(): Promise<SignalsStatus> {
  const response = await fetch("/api/people/signals")
  if (!response.ok) throw await failure(response, "Couldn't read signal refresh progress.")
  return body<SignalsStatus>(response)
}

export async function checkSignals(people: readonly string[]): Promise<SignalsPlan> {
  const response = await fetch("/api/people/signals/check", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ people }),
  })
  if (!response.ok) throw await failure(response, "Couldn't check the signal refresh cost.")
  return body<SignalsPlan>(response)
}

export async function refreshSignals(people: readonly string[], checked?: string): Promise<SignalsStatus> {
  const response = await fetch("/api/people/signals", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ people, checked }),
  })
  if (!response.ok) throw await failure(response, "Couldn't refresh signals.")
  return body<SignalsStatus>(response)
}
