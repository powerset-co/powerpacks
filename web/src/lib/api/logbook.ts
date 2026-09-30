// The People page's Logbook routes (share/web/logbook.py): every key is always sent.

import { body, failure } from "@/lib/api/http"

export type LogbookState = "idle" | "building" | "completed" | "failed"
export type ChannelState = "ok" | "missing" | "unreadable"

export interface ChannelCoverage {
  channel: string
  status: ChannelState
  // Messages this build wrote from the channel.
  messages: number
  // The store's oldest and newest message.
  earliest: string | null
  latest: string | null
}

export interface LogbookResult {
  root: string
  entries: string[]
  messages: number
  files: number
  channels: ChannelCoverage[]
  elapsed_seconds: number
}

export interface LogbookStatus {
  status: LogbookState
  // The parent ids of the build running or last run.
  people: string[]
  result: LogbookResult | null
  error: string | null
}

const URL = "/api/people/logbook"
export const LOGBOOK_DOWNLOAD_URL = `${URL}/download`
const FALLBACK = "Couldn't reach the logbook. Try again."

async function answer(response: Response): Promise<LogbookStatus> {
  if (!response.ok) throw await failure(response, FALLBACK)
  return body<LogbookStatus>(response)
}

export async function readLogbook(): Promise<LogbookStatus> {
  return answer(await fetch(URL))
}

/** Starts one build for these parents; a refusal (400, 409) throws the server's sentence. */
export async function buildLogbook(people: readonly string[]): Promise<LogbookStatus> {
  return answer(
    await fetch(URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ people }),
    }),
  )
}
