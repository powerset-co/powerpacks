import { describe, expect, it } from "vitest"

import { readScreenQuery, stageHref } from "./links"

describe("the page's URLs", () => {
  it("reads the query names", () => {
    expect(readScreenQuery("?stage=linkedin&view=yes&preview=1&debug=1&index=3")).toEqual({
      stage: "linkedin",
      view: "yes",
      preview: true,
      debug: true,
      index: 3,
    })
  })

  it("defaults to the store's stage, no preview, no debug, the first card", () => {
    expect(readScreenQuery("")).toEqual({ stage: "", view: "", preview: false, debug: false, index: 0 })
    expect(readScreenQuery("?preview=true&debug=yes&index=x").preview).toBe(false)
    expect(readScreenQuery("?index=-2").index).toBe(0)
  })

  it("opens a stage's screen without preview", () => {
    expect(stageHref("linkedin")).toBe("/?stage=linkedin")
  })
})
