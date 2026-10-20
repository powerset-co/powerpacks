// The Memory page's JSON routes on the Python review server (packs/shared/web/memory.py).

import { body, failure } from "@/lib/api/http"
import type { MemoryJob, MemoryMeaning, MemoryPeoplePage, MemorySelector } from "@/types/memory"

export const MEMORY_KEY = ["memory"]

export async function fetchMemory(signal?: AbortSignal): Promise<MemoryJob> {
  const response = await fetch("/api/memory", { signal })
  if (!response.ok) throw await failure(response, "Couldn't load Memory.")
  return body<MemoryJob>(response)
}

async function post(path: string, payload: object, fallback: string): Promise<MemoryJob> {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  })
  if (!response.ok) throw await failure(response, fallback)
  return body<MemoryJob>(response)
}

export function enableMemory(approvedSpendUsd: number): Promise<MemoryJob> {
  return post("/api/memory/enable", { approved_spend_usd: approvedSpendUsd }, "Couldn't enable Memory.")
}

/** The same run again, incremental, recorded failures first, under the spend already approved. */
export function updateMemory(): Promise<MemoryJob> {
  return post("/api/memory/update", {}, "Couldn't update Memory.")
}

export function setKeepUpdated(keepUpdated: boolean): Promise<MemoryJob> {
  return post("/api/memory/settings", { keep_updated: keepUpdated }, "Couldn't change the setting.")
}

export async function searchMemory(q: string, signal?: AbortSignal): Promise<MemoryMeaning[]> {
  const response = await fetch(`/api/memory/search?${new URLSearchParams({ q })}`, { signal })
  if (!response.ok) throw await failure(response, "Couldn't search Memory.")
  return body<MemoryMeaning[]>(response)
}

export async function memoryPeople(
  selector: MemorySelector,
  after: string,
  signal?: AbortSignal,
): Promise<MemoryPeoplePage> {
  const params = new URLSearchParams({ ...selector, after, limit: "50" })
  const response = await fetch(`/api/memory/people?${params}`, { signal })
  if (!response.ok) throw await failure(response, "Couldn't list these people.")
  return body<MemoryPeoplePage>(response)
}
