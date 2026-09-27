import { describe, expect, it } from "vitest"

import { RATINGS } from "./fixture"
import { rubricChoices } from "./rubric"

describe("rubricChoices", () => {
  it("orders the choices and splits each meaning at the dash", () => {
    const choices = rubricChoices({ "5": RATINGS.rubric["5"] ?? "", "1": "Clear no" })
    expect(choices).toEqual([
      { score: 1, name: "Clear no", detail: "", meaning: "Clear no" },
      {
        score: 5,
        name: "Strong yes",
        detail: "Particularly compelling",
        meaning: "Strong yes — Particularly compelling",
      },
    ])
  })
})
