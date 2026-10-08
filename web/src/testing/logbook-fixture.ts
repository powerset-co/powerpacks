// Synthetic Logbook statuses and saved entries, every key present as the routes send them.

import type {
  LogbookConversation,
  LogbookEntry,
  LogbookEntryDetail,
  LogbookMessage,
  LogbookParticipant,
  LogbookResult,
  LogbookStatus,
} from "@/lib/api/logbook"

export const RESULT: LogbookResult = {
  root: ".powerpacks/logbook",
  entries: ["casey-delta-p2", "family"],
  messages: 12,
  files: 3,
  channels: [
    { channel: "gmail", status: "missing", messages: 0, earliest: null, latest: null },
    { channel: "imessage", status: "unreadable", messages: 0, earliest: null, latest: null },
    {
      channel: "whatsapp",
      status: "ok",
      messages: 12,
      earliest: "2015-03-01T00:00:00Z",
      latest: "2026-09-01T00:00:00Z",
    },
  ],
  elapsed_seconds: 0.4,
}

export function logbookStatus(overrides: Partial<LogbookStatus> = {}): LogbookStatus {
  return { status: "idle", people: [], result: null, error: null, ...overrides }
}

export function savedEntry(overrides: Partial<LogbookEntry> = {}): LogbookEntry {
  return {
    slug: "casey-delta-p2",
    name: "Casey Delta",
    kind: "person",
    parent_id: "p2",
    messages: 3,
    channels: ["whatsapp"],
    first_at: "2024-09-03T14:05:00Z",
    last_at: "2024-09-04T09:00:00Z",
    ...overrides,
  }
}

export const DM: LogbookConversation = {
  path: "casey-delta-p2/whatsapp/dm.md",
  channel: "whatsapp",
  kind: "dm",
  title: "Casey Delta",
  messages: 3,
  first_at: "2024-09-03T14:05:00Z",
  last_at: "2024-09-04T09:00:00Z",
}

export const THREAD: LogbookConversation = {
  path: "casey-delta-p2/gmail/2019-dinner-plans-a1b2c3.md",
  channel: "gmail",
  kind: "thread",
  title: "Dinner plans",
  messages: 1,
  first_at: "2019-03-01T10:00:00Z",
  last_at: "2019-03-01T10:00:00Z",
}

export function entryDetail(entry: LogbookEntry, conversations: LogbookConversation[]): LogbookEntryDetail {
  return { ...entry, conversations }
}

// Untrusted text: markup must show as written.
export const MESSAGES: LogbookMessage[] = [
  { at: "2024-09-03 14:05", sender: "Casey Delta", text: "<b>hi</b> <img src=x onerror=alert(1)>" },
  { at: "2024-09-03 14:06", sender: "Casey Delta", text: "second line\nthird line" },
  { at: "2024-09-04 09:00", sender: "me", text: "" },
]

/** The Gmail thread's people as the mail store lists them. */
export const THREAD_PEOPLE: LogbookParticipant[] = [
  { name: "Casey Delta", email: "casey@example.com", roles: ["from", "to"] },
  { name: "Jordan Bravo", email: "jordan@example.com", roles: ["to"] },
  { name: "Riley Echo", email: "riley@example.com", roles: ["cc"] },
]

/** Two messages on the 1st of each of `months` months from January of `year`. */
export function monthlyMessages(year: number, months: number): LogbookMessage[] {
  return Array.from({ length: months }, (_, offset) => {
    const y = year + Math.floor(offset / 12)
    const m = String((offset % 12) + 1).padStart(2, "0")
    return [
      { at: `${y}-${m}-01 09:00`, sender: "Casey Delta", text: `Hello ${y}-${m}` },
      { at: `${y}-${m}-01 09:05`, sender: "me", text: `Reply ${y}-${m}` },
    ]
  }).flat()
}
