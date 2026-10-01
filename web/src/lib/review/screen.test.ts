import { describe, expect, it } from "vitest"

import { reviewPage } from "@/testing/review-fixture"

import { enrichPanelShown } from "./screen"

describe("enrichPanelShown", () => {
  it("holds on the Enrich screen unless the synthesis handoff replaced it", () => {
    expect(enrichPanelShown(reviewPage("enrich"))).toBe(true)
    expect(enrichPanelShown(reviewPage("enrich", { needs_synthesis: true }))).toBe(false)
    expect(enrichPanelShown(reviewPage("done"))).toBe(false)
  })
})
