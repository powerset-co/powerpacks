// The People page's Logbook routes: the build (share/web/logbook.py) and the saved archive
// (share/web/logbook_archive.py). Every key is always sent.

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

/** One saved person or group, as GET /api/people/logbook/entries lists it. */
export interface LogbookEntry {
  slug: string
  name: string
  kind: "person" | "group"
  // The People parent whose build writes this entry; null for groups.
  parent_id: string | null
  messages: number
  channels: string[]
  first_at: string | null
  last_at: string | null
}

/** One file of an entry: a Gmail thread, a DM or a group chat. */
export interface LogbookConversation {
  // The key the conversation route reads it by.
  path: string
  channel: string
  kind: "thread" | "dm" | "group"
  title: string
  messages: number
  first_at: string | null
  last_at: string | null
}

/** GET /api/people/logbook/entry: the entry and its conversations, most recent first. */
export interface LogbookEntryDetail extends LogbookEntry {
  conversations: LogbookConversation[]
}

/** One message as the archive saved it: `at` is "YYYY-MM-DD HH:MM", "" when undated. */
export interface LogbookMessage {
  at: string
  sender: string
  text: string
}

/** Someone on a saved Gmail thread, from the local mail store: `roles` are from, to, cc, bcc. */
export interface LogbookParticipant {
  name: string
  email: string
  roles: string[]
}

/** GET /api/people/logbook/conversation. `participants` is null when the thread is not Gmail
 *  or the local mail store can't say. */
export interface LogbookConversationBody {
  messages: LogbookMessage[]
  participants: LogbookParticipant[] | null
}

const URL = "/api/people/logbook"
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

async function read<T>(path: string, query: Record<string, string>, signal?: AbortSignal): Promise<T> {
  const response = await fetch(`${URL}/${path}?${new URLSearchParams(query).toString()}`, { signal })
  if (!response.ok) throw await failure(response, FALLBACK)
  return body<T>(response)
}

/** Every saved entry; none before the first build. */
export async function fetchLogbookEntries(): Promise<LogbookEntry[]> {
  return (await read<{ entries: LogbookEntry[] }>("entries", {})).entries
}

export async function fetchLogbookEntry(slug: string, signal?: AbortSignal): Promise<LogbookEntryDetail> {
  return read<LogbookEntryDetail>("entry", { slug }, signal)
}

export async function fetchConversation(
  slug: string,
  path: string,
  signal?: AbortSignal,
): Promise<LogbookConversationBody> {
  return read<LogbookConversationBody>("conversation", { slug, path }, signal)
}
