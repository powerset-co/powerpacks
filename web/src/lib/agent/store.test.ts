import { describe, expect, it } from "vitest"

import { EMPTY_AGENT, reduce, type AgentState } from "./store"

const RUNNING: AgentState = { ...EMPTY_AGENT, threadId: "t1", running: true, turnId: "u1" }

describe("reduce", () => {
  it("streams a message into its entry", () => {
    const started = reduce(RUNNING, {
      type: "item",
      threadId: "t1",
      entry: { kind: "agent", id: "i1", text: "" },
    })
    const streamed = reduce(started, {
      type: "delta",
      threadId: "t1",
      itemId: "i1",
      field: "text",
      delta: "Found 3",
    })
    expect(streamed.entries).toEqual([{ kind: "agent", id: "i1", text: "Found 3" }])
  })

  it("replaces an item when it completes", () => {
    const command = {
      kind: "command",
      id: "c1",
      command: "ls",
      exitCode: null,
      output: "",
      durationMs: null,
    } as const
    const started = reduce(RUNNING, {
      type: "item",
      threadId: "t1",
      entry: { ...command, status: "running" },
    })
    const done = reduce(started, {
      type: "item",
      threadId: "t1",
      entry: { ...command, status: "done", output: "a\n" },
    })
    expect(done.entries).toEqual([{ ...command, status: "done", output: "a\n" }])
  })

  it("ignores another thread's events", () => {
    const other = reduce(RUNNING, {
      type: "item",
      threadId: "t2",
      entry: { kind: "agent", id: "x", text: "no" },
    })
    expect(other).toBe(RUNNING)
  })

  it("ends the turn and shows its error", () => {
    const ended = reduce(RUNNING, { type: "turnCompleted", threadId: "t1", error: "Usage limit reached" })
    expect(ended.running).toBe(false)
    expect(ended.entries.at(-1)).toMatchObject({ kind: "error", text: "Usage limit reached" })
  })

  it("waits out a retried error", () => {
    expect(reduce(RUNNING, { type: "error", threadId: "t1", message: "reconnecting", willRetry: true })).toBe(
      RUNNING,
    )
  })
})
