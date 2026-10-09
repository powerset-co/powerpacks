// The Searches page's JSON routes and saved tags on the Python review server.

import { body, failure } from "@/lib/api/http"
import type { CatalogPayload, SearchRunPayload, Tagged, TagsPayload } from "@/types/searches"

const API = "/searches"

export async function fetchCatalog(signal?: AbortSignal): Promise<CatalogPayload> {
  const response = await fetch(`${API}/api/catalog`, { signal })
  if (!response.ok) throw await failure(response, "Couldn't load searches.")
  return body<CatalogPayload>(response)
}

export async function fetchSearchRun(runId: string, signal?: AbortSignal): Promise<SearchRunPayload> {
  const response = await fetch(`${API}/api/search.json?run_id=${encodeURIComponent(runId)}`, { signal })
  if (!response.ok) throw await failure(response, "Couldn't load search.")
  return body<SearchRunPayload>(response)
}

export async function fetchTags(runId: string): Promise<TagsPayload> {
  const response = await fetch(`${API}/tags?run_id=${encodeURIComponent(runId)}`)
  if (!response.ok) throw await failure(response, "Couldn't load tags.")
  return body<TagsPayload>(response)
}

export async function writeTags(runId: string, tagged: Tagged): Promise<{ ok: boolean }> {
  const response = await fetch(`${API}/tags`, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({ run_id: runId, person_id: "", comment: "", tagged: JSON.stringify(tagged) }),
  })
  if (!response.ok) throw await failure(response, "Couldn't save tags.")
  return body<{ ok: boolean }>(response)
}

export interface AskOwner {
  operator_id: string
  name: string
}

export interface AskPreview {
  pinned: { public_identifier: string; linkedin_url: string; name: string; local_rank: number }[]
  skipped: number
  candidates: { public_identifier: string; name: string; owners: AskOwner[] }[]
  operators: { operator_id: string; name: string; candidates: number }[]
}

export interface AskAnswer {
  verdict: "recommend" | "not_fit" | "unsure"
  reason: string
  can_intro: boolean
  relationship: string
  last_contact: string | null
  confidence: number
}

export interface SentAsk {
  ask_id: string
  question: string
  candidates: { public_identifier: string; owners: string[] }[]
}

export interface AskStatus {
  ask: SentAsk | null
  answers?: {
    pending: number
    candidates: {
      public_identifier: string
      name: string
      owners: {
        operator_id: string
        name: string
        status: string
        awake: boolean
        answer: AskAnswer | null
      }[]
    }[]
  }
}

export async function fetchAskPreview(runId: string): Promise<AskPreview> {
  const response = await fetch(`${API}/api/ask/preview?run_id=${encodeURIComponent(runId)}`)
  if (!response.ok) throw await failure(response, "Couldn't load who would be asked.")
  return body<AskPreview>(response)
}

export async function fetchAskStatus(runId: string): Promise<AskStatus> {
  const response = await fetch(`${API}/api/ask/status?run_id=${encodeURIComponent(runId)}`)
  if (!response.ok) throw await failure(response, "Couldn't load the answers.")
  return body<AskStatus>(response)
}

export async function sendAsk(
  runId: string,
  question: string,
): Promise<{ status: string; ask: SentAsk | null }> {
  const response = await fetch(`${API}/ask`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ run_id: runId, question }),
  })
  if (!response.ok) throw await failure(response, "Couldn't send the ask.")
  return body(response)
}
