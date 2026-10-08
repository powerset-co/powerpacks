import { describe, expect, it } from "vitest"

import { decodePeople } from "@/lib/api/people"
import { DM, MESSAGES, THREAD, THREAD_PEOPLE, monthlyMessages, savedEntry } from "@/testing/logbook-fixture"
import { PAYLOAD } from "@/testing/people-fixture"

import {
  conversationMeta,
  conversationTitle,
  filterConversations,
  messageSenders,
  monthsByYear,
  participantRoles,
  readerMonths,
  readerHref,
  readerItems,
  readerScope,
  scopedEntries,
  span,
  withLogbooks,
} from "./logbook"

describe("reader route", () => {
  it("names each entry in the query and reads them back in order", () => {
    const href = readerHref(["casey-delta-p2", "family & friends"])
    expect(href).toBe("/people/logbook?entry=casey-delta-p2&entry=family+%26+friends")
    expect(readerScope(new URLSearchParams(href.split("?")[1]))).toEqual([
      "casey-delta-p2",
      "family & friends",
    ])
    expect(readerHref([])).toBe("/people/logbook")
  })

  it("keeps the scope's order and drops unsaved slugs; unscoped is most recent first", () => {
    const old = savedEntry({ slug: "old", last_at: "2019-01-01T00:00:00Z" })
    const recent = savedEntry({ slug: "recent", last_at: "2025-01-01T00:00:00Z" })
    const undated = savedEntry({ slug: "undated", last_at: null })
    expect(scopedEntries([old, recent], ["old", "gone", "recent"]).map((e) => e.slug)).toEqual([
      "old",
      "recent",
    ])
    expect(scopedEntries([old, undated, recent], []).map((e) => e.slug)).toEqual(["recent", "old", "undated"])
  })
})

describe("withLogbooks", () => {
  it("gives each parent its saved entry and everyone else none; groups name no parent", () => {
    const rows = withLogbooks(decodePeople(PAYLOAD), [
      savedEntry(),
      savedEntry({ slug: "family", kind: "group", parent_id: null }),
    ])
    expect(rows.map((row) => [row.parent_id, row.logbook])).toEqual([
      ["p1", ""],
      ["p2", "casey-delta-p2"],
      ["p3", ""],
      ["p4", ""],
    ])
  })
})

describe("readerItems", () => {
  it("puts a day line before each day and names the sender when it changes", () => {
    const items = readerItems(MESSAGES)
    expect(
      items.map((item) => (item.kind === "day" ? `day ${item.label}` : `${item.sender ?? "·"} ${item.time}`)),
    ).toEqual([
      "day Tue, Sep 3, 2024",
      "Casey Delta 2:05 PM",
      "· 2:06 PM",
      "day Wed, Sep 4, 2024",
      "You 9:00 AM",
    ])
  })

  it("keeps undated messages under No date and starts a new sender run after a day line", () => {
    const items = readerItems([
      { at: "", sender: "Casey Delta", text: "a" },
      { at: "2024-09-03", sender: "Casey Delta", text: "b" },
    ])
    expect(items.map((item) => (item.kind === "day" ? item.label : [item.sender, item.time]))).toEqual([
      "No date",
      ["Casey Delta", ""],
      "Tue, Sep 3, 2024",
      ["Casey Delta", ""],
    ])
  })
})

describe("words", () => {
  it("spans months and describes a conversation", () => {
    expect(span("2019-03-01T10:00:00Z", "2019-03-20T10:00:00Z")).toBe("Mar 2019")
    expect(span(null, null)).toBe("")
    expect(conversationMeta(DM)).toBe("3 messages · Sep 2024")
    expect(conversationTitle(DM)).toBe("WhatsApp direct messages")
    expect(conversationTitle({ ...THREAD, title: "" })).toBe("No subject")
  })
})

describe("timeline", () => {
  it("places each month at its first day line and counts its messages", () => {
    const items = readerItems(monthlyMessages(2023, 14))
    const months = readerMonths(items)
    expect(months).toHaveLength(14)
    expect(months[0]).toMatchObject({ key: "2023-01", year: "2023", label: "Jan", index: 0, messages: 2 })
    expect(months[13]).toMatchObject({ key: "2024-02", label: "Feb", index: 39 })
    expect(items[39]).toMatchObject({ kind: "day", label: "Thu, Feb 1, 2024" })
    expect(monthsByYear(months).map(([year, rows]) => [year, rows.length])).toEqual([
      ["2023", 12],
      ["2024", 2],
    ])
  })

  it("keeps a month appended out of order at its first place, and undated messages as No date", () => {
    const months = readerMonths(
      readerItems([
        { at: "2024-01-02 10:00", sender: "me", text: "a" },
        { at: "2024-02-02 10:00", sender: "me", text: "b" },
        { at: "2024-01-05 10:00", sender: "me", text: "backfilled" },
        { at: "", sender: "me", text: "undated" },
      ]),
    )
    expect(months.map(({ key, label, messages }) => [key, label, messages])).toEqual([
      ["2024-01", "Jan", 2],
      ["2024-02", "Feb", 1],
      ["undated", "No date", 1],
    ])
  })
})

describe("rail filter and people", () => {
  it("filters conversations by name and channel", () => {
    expect(filterConversations([DM, THREAD], "DINNER", new Set())).toEqual([THREAD])
    expect(filterConversations([DM, THREAD], "", new Set(["whatsapp"]))).toEqual([DM])
    expect(filterConversations([DM, THREAD], "direct", new Set(["gmail"]))).toEqual([])
  })

  it("lists the mail store's people by role and senders once each", () => {
    expect(
      participantRoles(THREAD_PEOPLE).map(({ role, people }) => [role, people.map((p) => p.name)]),
    ).toEqual([
      ["From", ["Casey Delta"]],
      ["To", ["Casey Delta", "Jordan Bravo"]],
      ["Cc", ["Riley Echo"]],
    ])
    expect(messageSenders(MESSAGES)).toEqual(["Casey Delta", "You"])
  })
})
