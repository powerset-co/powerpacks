import { describe, expect, it } from "vitest"

import { reviewStatus } from "@/testing/review-fixture"

import { decideStatus, type StatusInput } from "./sync"

/** The Enrich screen, opened by the flow, with one status already read at "enrich". */
function input(overrides: Partial<StatusInput> = {}): StatusInput {
  return {
    view: "enrich",
    preview: false,
    hasDraft: false,
    lastStage: "enrich",
    status: reviewStatus(),
    loadedToken: "token-1",
    ...overrides,
  }
}

describe("decideStatus", () => {
  it("does nothing while the server is where the screen is and the token is the same", () => {
    expect(decideStatus(input())).toEqual({ action: { kind: "nothing" }, lastStage: "enrich" })
  })

  it("moves forward on a stage change observed while the screen was open", () => {
    const decision = decideStatus(
      input({ status: reviewStatus({ stage: "linkedin", state_token: "token-2" }) }),
    )
    expect(decision).toEqual({ action: { kind: "navigate", stage: "linkedin" }, lastStage: "linkedin" })
  })

  it("moves to done from an earlier screen", () => {
    const decision = decideStatus(input({ lastStage: "linkedin", status: reviewStatus({ stage: "done" }) }))
    expect(decision.action).toEqual({ kind: "navigate", stage: "done" })
  })

  it("never moves on the first read: a difference that existed when the screen opened is the user's choice", () => {
    const status = reviewStatus({ stage: "linkedin" })
    expect(decideStatus(input({ lastStage: "", status }))).toEqual({
      action: { kind: "nothing" },
      lastStage: "linkedin",
    })
  })

  it("reloads Done on the first read when only the token differs", () => {
    const status = reviewStatus({ stage: "done", state_token: "token-2" })
    expect(decideStatus(input({ view: "done", lastStage: "", status })).action).toEqual({ kind: "reload" })
  })

  it("never moves backward; a changed token reloads Done instead", () => {
    const back = reviewStatus({ stage: "worth", state_token: "token-2" })
    expect(decideStatus(input({ view: "done", lastStage: "done", status: back }))).toEqual({
      action: { kind: "reload" },
      lastStage: "worth",
    })
    expect(decideStatus(input({ status: reviewStatus({ stage: "worth" }) })).action).toEqual({
      kind: "nothing",
    })
  })

  it("never moves a preview screen", () => {
    const ahead = reviewStatus({ stage: "linkedin", state_token: "token-2" })
    expect(decideStatus(input({ preview: true, status: ahead })).action).toEqual({ kind: "nothing" })
  })

  it("does not move when the server is at the screen's own stage again", () => {
    const decision = decideStatus(input({ lastStage: "worth", status: reviewStatus({ stage: "enrich" }) }))
    expect(decision).toEqual({ action: { kind: "nothing" }, lastStage: "enrich" })
  })

  it("reloads Done when the state token changed under the screen", () => {
    const decision = decideStatus(
      input({
        view: "done",
        lastStage: "done",
        status: reviewStatus({ stage: "done", state_token: "token-2" }),
      }),
    )
    expect(decision.action).toEqual({ kind: "reload" })
  })

  it("never yanks a typed draft: no move and no reload, and the observed stage is still kept", () => {
    const ahead = reviewStatus({ stage: "linkedin", state_token: "token-2" })
    expect(decideStatus(input({ hasDraft: true, status: ahead }))).toEqual({
      action: { kind: "nothing" },
      lastStage: "linkedin",
    })
    const stale = reviewStatus({ state_token: "token-2" })
    expect(decideStatus(input({ hasDraft: true, status: stale })).action).toEqual({ kind: "nothing" })
  })

  it("does not reload on an empty token", () => {
    expect(decideStatus(input({ status: reviewStatus({ state_token: "" }) })).action).toEqual({
      kind: "nothing",
    })
  })
})

describe("decideStatus on the waiting screen", () => {
  it("does not reload Enrich when the store changes under it: the status itself is what it shows", () => {
    expect(decideStatus(input({ status: reviewStatus({ state_token: "token-2" }) })).action).toEqual({
      kind: "nothing",
    })
  })

  it("still reloads Done when the store changes under it", () => {
    const done = input({
      view: "done",
      lastStage: "done",
      status: reviewStatus({ stage: "done", state_token: "token-2" }),
    })
    expect(decideStatus(done).action).toEqual({ kind: "reload" })
  })
})
