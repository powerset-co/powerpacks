import { describe, expect, it } from "vitest"

import { decisionProgress } from "@/testing/review-fixture"

import { panelOf } from "./panel"
import { otherPile, tabCounts } from "./piles"
import { personNamed } from "./worth-fixture"

describe("tabCounts", () => {
  const progress = decisionProgress({ worth_pending: 3, worth_yes: 5, worth_no: 2 })

  it("shows the server's counts when nothing is saving", () => {
    expect(tabCounts(progress, [])).toEqual({ review: 3, yes: 5, no: 2 })
  })

  it("moves one person per decision still saving", () => {
    expect(tabCounts(progress, [{ from: "review", to: "yes" }])).toEqual({ review: 2, yes: 6, no: 2 })
    expect(
      tabCounts(progress, [
        { from: "review", to: "yes" },
        { from: "review", to: "no" },
        { from: "yes", to: "no" },
      ]),
    ).toEqual({ review: 1, yes: 5, no: 4 })
  })

  it("never counts below zero", () => {
    const empty = decisionProgress({ worth_pending: 0, worth_yes: 0, worth_no: 0 })
    expect(tabCounts(empty, [{ from: "review", to: "yes" }])).toEqual({ review: 0, yes: 1, no: 0 })
  })
})

describe("piles", () => {
  it("flips a pile to the other", () => {
    expect([otherPile("yes"), otherPile("no")]).toEqual(["no", "yes"])
  })
})

describe("panelOf", () => {
  const person = personNamed("Casey Delta")

  it("is the person's card, with the carousel's position when the server sends one", () => {
    const card = { person, candidate: null }
    expect(panelOf({ card, synthesize_pending: false, queue: null })).toEqual({
      kind: "card",
      person,
      candidate: null,
      queue: null,
    })
    const queue = { index: 1, total: 3 }
    expect(panelOf({ card, synthesize_pending: false, queue })).toMatchObject({ kind: "card", queue })
  })

  it("is the synthesis handoff when there is no card because synthesis has not run", () => {
    expect(panelOf({ card: null, synthesize_pending: true, queue: null })).toEqual({ kind: "synthesis" })
  })

  it("is empty when the queue has nobody left", () => {
    expect(panelOf({ card: null, synthesize_pending: false, queue: null })).toEqual({ kind: "empty" })
  })
})
