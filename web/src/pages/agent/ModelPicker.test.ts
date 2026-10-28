import { describe, expect, it } from "vitest"

import { frontier } from "./ModelPicker"

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
