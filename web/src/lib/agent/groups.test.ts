import { describe, expect, it } from "vitest"

import { groupThreads } from "./groups"

const NOW = new Date(2026, 9, 8, 15, 0)
const chat = (id: string, date: Date) => ({ id, title: id, updatedAt: date })

describe("groupThreads", () => {
  it("files chats by age, newest first, and drops empty groups", () => {
    const groups = groupThreads(
      [
        chat("old", new Date(2026, 5, 1)),
        chat("morning", new Date(2026, 9, 8, 9)),
        chat("yesterday", new Date(2026, 9, 7, 20)),
        chat("noon", new Date(2026, 9, 8, 12)),
      ],
      NOW,
    )
    expect(groups.map(({ label, threads }) => [label, threads.map(({ id }) => id)])).toEqual([
      ["Today", ["noon", "morning"]],
      ["Yesterday", ["yesterday"]],
      ["Older", ["old"]],
    ])
  })
})
