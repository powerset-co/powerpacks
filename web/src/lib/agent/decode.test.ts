import { describe, expect, it } from "vitest"

import { decodeNotification, decodeRequest, decodeStatus } from "./decode"

describe("decodeStatus", () => {
  it("reads a ChatGPT account", () => {
    const raw = {
      installed: true,
      account: { type: "chatgpt", email: "casey@example.com", planType: "plus" },
    }
    expect(decodeStatus(raw)).toEqual({
      installed: true,
      account: { kind: "chatgpt", email: "casey@example.com", plan: "plus" },
    })
  })

  it("reads a signed-out and a missing Codex", () => {
    expect(decodeStatus({ installed: true, account: null })).toEqual({ installed: true, account: null })
    expect(decodeStatus({ installed: false })).toEqual({ installed: false })
  })
})

describe("decodeNotification", () => {
  it("reads a finished command", () => {
    const raw = {
      method: "item/completed",
      params: {
        threadId: "t1",
        item: {
          type: "commandExecution",
          id: "i1",
          command: "bin/doctor",
          status: "failed",
          exitCode: 2,
          aggregatedOutput: "boom",
          durationMs: 1200,
        },
      },
    }
    expect(decodeNotification(raw)).toEqual({
      type: "item",
      threadId: "t1",
      entry: {
        kind: "command",
        id: "i1",
        command: "bin/doctor",
        status: "failed",
        exitCode: 2,
        output: "boom",
        durationMs: 1200,
      },
    })
  })

  it("reads message and output deltas", () => {
    const message = {
      method: "item/agentMessage/delta",
      params: { threadId: "t1", itemId: "i2", delta: "Hi" },
    }
    const output = {
      method: "item/commandExecution/outputDelta",
      params: { threadId: "t1", itemId: "i1", delta: "ok\n" },
    }
    expect(decodeNotification(message)).toEqual({
      type: "delta",
      threadId: "t1",
      itemId: "i2",
      field: "text",
      delta: "Hi",
    })
    expect(decodeNotification(output)).toEqual({
      type: "delta",
      threadId: "t1",
      itemId: "i1",
      field: "output",
      delta: "ok\n",
    })
  })

  it("reads a turn's end with its error", () => {
    const raw = {
      method: "turn/completed",
      params: { threadId: "t1", turn: { id: "u1", error: { message: "Rate limited" } } },
    }
    expect(decodeNotification(raw)).toEqual({ type: "turnCompleted", threadId: "t1", error: "Rate limited" })
  })

  it("reads sign-in completion", () => {
    const raw = { method: "account/login/completed", params: { loginId: "l1", success: true, error: null } }
    expect(decodeNotification(raw)).toEqual({ type: "loginCompleted", success: true, error: null })
  })

  it("ignores what the page does not show", () => {
    expect(decodeNotification({ method: "thread/tokenUsage/updated", params: {} })).toBeNull()
    expect(
      decodeNotification({ method: "item/started", params: { item: { type: "userMessage", id: "u" } } }),
    ).toBeNull()
  })
})

describe("decodeRequest", () => {
  it("reads a command approval", () => {
    const raw = {
      id: 7,
      method: "item/commandExecution/requestApproval",
      params: { command: "uv sync", reason: "needs network" },
    }
    expect(decodeRequest(raw)).toEqual({
      kind: "command",
      requestId: 7,
      command: "uv sync",
      reason: "needs network",
    })
  })

  it("echoes the requested permissions as the grant", () => {
    const permissions = { network: { enabled: true }, fileSystem: null }
    const raw = {
      id: "r1",
      method: "item/permissions/requestApproval",
      params: { reason: null, permissions },
    }
    expect(decodeRequest(raw)).toEqual({
      kind: "permissions",
      requestId: "r1",
      reason: null,
      network: true,
      paths: [],
      grant: { network: { enabled: true } },
    })
  })
})
