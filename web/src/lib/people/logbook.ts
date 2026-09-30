// The Logbook reader's route and its reading of saved messages: days, times, senders.

import type { LogbookConversation, LogbookEntry, LogbookMessage, LogbookParticipant } from "@/lib/api/logbook"
import type { Person } from "@/types/people"
import { plural } from "@/lib/copy"
import { isRecord } from "@/lib/utils"

import { sentence } from "./copy"

/** Nested under /people so the People list stays mounted behind the reader. */
export const LOGBOOK_PATH = "/people/logbook"
const ENTRY_PARAM = "entry"

/** The reader over these saved entries; none reads every saved entry. */
export function readerHref(slugs: readonly string[]): string {
  const query = new URLSearchParams(slugs.map((slug) => [ENTRY_PARAM, slug])).toString()
  return query ? `${LOGBOOK_PATH}?${query}` : LOGBOOK_PATH
}

/** The rows with each parent's saved entry slug, "" for a parent without one. */
export function withLogbooks(rows: readonly Person[], entries: readonly LogbookEntry[]): Person[] {
  const saved = new Map(
    entries.flatMap(({ parent_id, slug }) => (parent_id ? [[parent_id, slug] as const] : [])),
  )
  return rows.map((row) => ({ ...row, logbook: saved.get(row.parent_id) ?? "" }))
}

/** The history state a reader opened from People carries: Back returns there. */
export interface ReaderState {
  fromPeople: boolean
}

export function openedFromPeople(state: unknown): boolean {
  return isRecord(state) && state.fromPeople === true
}

export function readerScope(params: URLSearchParams): string[] {
  return params.getAll(ENTRY_PARAM)
}

/** The entries in scope in the order asked; every entry, most recent first, when unscoped. */
export function scopedEntries(entries: readonly LogbookEntry[], scope: readonly string[]): LogbookEntry[] {
  if (scope.length) {
    const bySlug = new Map(entries.map((entry) => [entry.slug, entry]))
    return scope.flatMap((slug) => bySlug.get(slug) ?? [])
  }
  return [...entries].sort((a, b) => (b.last_at ?? "").localeCompare(a.last_at ?? ""))
}

// The producer's sender for the owner's own messages (logbook_sources.py).
const OWNER_SENDER = "me"

// Every item carries its month ("2024-09", or UNDATED) so the timeline can follow the scroll.
export type ReaderItem =
  | { kind: "day"; key: string; month: string; label: string }
  | {
      kind: "message"
      key: string
      month: string
      message: LogbookMessage
      time: string
      sender: string | null
    }

const UNDATED = "undated"

const DAY = new Intl.DateTimeFormat("en-US", {
  weekday: "short",
  month: "short",
  day: "numeric",
  year: "numeric",
  timeZone: "UTC",
})
const TIME = new Intl.DateTimeFormat("en-US", { hour: "numeric", minute: "2-digit", timeZone: "UTC" })

// "YYYY-MM-DD HH:MM" as the archive saved it, read as written: no timezone is converted.
const SAVED_AT = /^(\d{4})-(\d{2})-(\d{2})(?: (\d{2}):(\d{2}))?$/

function savedDate(at: string): { day: string; date: Date; timed: boolean } | null {
  const parts = SAVED_AT.exec(at)
  if (!parts) return null
  const [, year, month, day, hour, minute] = parts
  const date = new Date(
    Date.UTC(Number(year), Number(month) - 1, Number(day), Number(hour ?? 0), Number(minute ?? 0)),
  )
  return { day: at.slice(0, 10), date, timed: hour !== undefined }
}

export function senderName(sender: string): string {
  return sender === OWNER_SENDER ? "You" : sender
}

/**
 * The conversation as the reader lists it: a day line before each new day, and the sender
 * named only when it changes within a day. Undated messages keep their place under "No date".
 */
export function readerItems(messages: readonly LogbookMessage[]): ReaderItem[] {
  const items: ReaderItem[] = []
  let lastDay: string | null = null
  let lastSender: string | null = null
  messages.forEach((message, position) => {
    const saved = savedDate(message.at)
    const day = saved?.day ?? UNDATED
    const month = saved ? day.slice(0, 7) : UNDATED
    if (day !== lastDay) {
      items.push({
        kind: "day",
        key: `day-${position}`,
        month,
        label: saved ? DAY.format(saved.date) : "No date",
      })
      lastDay = day
      lastSender = null
    }
    items.push({
      kind: "message",
      key: `message-${position}`,
      month,
      message,
      time: saved?.timed ? TIME.format(saved.date) : "",
      sender: message.sender === lastSender ? null : senderName(message.sender),
    })
    lastSender = message.sender
  })
  return items
}

/** One month of a conversation on the timeline, where it first appears in the reader's items. */
export interface ReaderMonth {
  key: string
  // "2024", or "" for undated messages.
  year: string
  // "Sep", or "No date".
  label: string
  // The item the jump lands on: the month's first day line.
  index: number
  messages: number
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

/** Each month the saved messages fall in, in reading order; a month appended out of date order
 *  (sync adds backfilled history at the end) keeps its first position and counts every message. */
export function readerMonths(items: readonly ReaderItem[]): ReaderMonth[] {
  const months = new Map<string, ReaderMonth>()
  items.forEach((item, index) => {
    let month = months.get(item.month)
    if (!month) {
      const undated = item.month === UNDATED
      month = {
        key: item.month,
        year: undated ? "" : item.month.slice(0, 4),
        label: undated ? "No date" : (MONTHS[Number(item.month.slice(5, 7)) - 1] ?? item.month),
        index,
        messages: 0,
      }
      months.set(item.month, month)
    }
    if (item.kind === "message") month.messages += 1
  })
  return [...months.values()]
}

/** The months under their years, in reading order. */
export function monthsByYear(months: readonly ReaderMonth[]): [string, ReaderMonth[]][] {
  const years = new Map<string, ReaderMonth[]>()
  for (const month of months) years.set(month.year, [...(years.get(month.year) ?? []), month])
  return [...years]
}

// Months of saved timestamps in UTC, as the messages' own days are read.
const MONTH_YEAR = new Intl.DateTimeFormat("en-US", { month: "short", year: "numeric", timeZone: "UTC" })

function savedMonth(value: string): string {
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : MONTH_YEAR.format(date)
}

/** "Mar 2019 – Sep 2026", one month when both ends share it, "" with no dates. */
export function span(first: string | null, last: string | null): string {
  const from = first ? savedMonth(first) : ""
  const to = last ? savedMonth(last) : ""
  if (!from || !to || from === to) return from || to
  return `${from} – ${to}`
}

/** A conversation's name: the thread subject, the group's name, or "WhatsApp direct messages". */
export function conversationTitle({ kind, title, channel }: LogbookConversation): string {
  if (kind === "dm") return `${sentence(channel)} direct messages`
  if (title) return title
  return kind === "group" ? "Group chat" : "No subject"
}

/** "312 messages · Mar 2019 – Sep 2026". */
export function conversationMeta(conversation: LogbookConversation): string {
  const when = span(conversation.first_at, conversation.last_at)
  return [plural(conversation.messages, "message"), when].filter(Boolean).join(" · ")
}

/** The conversations whose name holds the text and whose channel is picked (none picked: any). */
export function filterConversations(
  conversations: readonly LogbookConversation[],
  text: string,
  channels: ReadonlySet<string>,
): LogbookConversation[] {
  const needle = text.trim().toLowerCase()
  return conversations.filter(
    (conversation) =>
      (!channels.size || channels.has(conversation.channel)) &&
      (!needle || conversationTitle(conversation).toLowerCase().includes(needle)),
  )
}

const ROLES = [
  ["from", "From"],
  ["to", "To"],
  ["cc", "Cc"],
  ["bcc", "Bcc"],
] as const

/** A thread's people by role, as the mail store lists them: From, To, Cc, Bcc; empty roles left out. */
export function participantRoles(
  participants: readonly LogbookParticipant[],
): { role: string; people: LogbookParticipant[] }[] {
  return ROLES.map(([role, label]) => ({
    role: label,
    people: participants.filter((person) => person.roles.includes(role)),
  })).filter(({ people }) => people.length)
}

/** Everyone who wrote in the saved messages, once each in first-seen order. */
export function messageSenders(messages: readonly LogbookMessage[]): string[] {
  return [...new Set(messages.map((message) => senderName(message.sender)))]
}

/** "Person · 1,204 messages" / "Group · 88 messages". */
export function entryMeta(entry: LogbookEntry): string {
  return `${entry.kind === "group" ? "Group" : "Person"} · ${plural(entry.messages, "message")}`
}
