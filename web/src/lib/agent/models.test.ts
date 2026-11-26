import { describe, expect, it } from "vitest"

import { frontier, resolveChoice } from "./models"

describe("frontier", () => {
  it("keeps the newest two generations and hides the rest", () => {
    const ids = [
      "gpt-6.1-sol",
      "gpt-6-astra",
      "gpt-6.1-terra",
      "gpt-6-sol",
      "gpt-6-luna",
      "gpt-5.6-sol",
      "gpt-5.6-terra",
    ]
    expect(frontier(ids.map((id) => ({ id }))).map((model) => model.id)).toEqual([
      "gpt-6.1-sol",
      "gpt-6-astra",
      "gpt-6.1-terra",
      "gpt-6-sol",
      "gpt-6-luna",
    ])
  })
  it("shows everything when there is nothing older", () => {
    const ids = ["gpt-6.1-sol", "gpt-6-astra"]
    expect(frontier(ids.map((id) => ({ id }))).length).toBe(2)
  })
})

describe("resolveChoice", () => {
  const efforts = ["low", "medium", "high"].map((id) => ({ id, description: "" }))
  const model = (id: string, isDefault = false) => ({
    id,
    name: id,
    description: "",
    isDefault,
    defaultEffort: "low",
    efforts,
  })
  const list = [model("gpt-6.1-terra", true), model("gpt-6.1-sol")]

  it("never runs the user's own Codex model when the account doesn't list it", () => {
    const choice = resolveChoice(
      list,
      { model: null, effort: null },
      { model: "gpt-6.2-astra", effort: "high" },
    )
    expect(choice?.model.id).toBe("gpt-6.1-terra")
    expect(choice?.effort).toBe("high")
  })
  it("drops a remembered pick the account no longer lists", () => {
    const choice = resolveChoice(
      list,
      { model: "gpt-6.2-astra", effort: "xhigh" },
      { model: null, effort: null },
    )
    expect(choice).toEqual({ model: list[0], effort: "low" })
  })
  it("keeps a listed pick and effort", () => {
    const choice = resolveChoice(
      list,
      { model: "gpt-6.1-sol", effort: "medium" },
      { model: "gpt-6.1-terra", effort: "high" },
    )
    expect(choice).toEqual({ model: list[1], effort: "medium" })
  })
  it("is null with nothing listed", () => {
    expect(resolveChoice([], { model: null, effort: null }, undefined)).toBeNull()
  })
})
