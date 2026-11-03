import { describe, expect, it } from "vitest"

import { LAST, lastBucket } from "./facets"

// The tab, facets, search and sort run on the server now: tests/test_people_page.py.

describe("lastBucket", () => {
  it("splits at a year and two years", () => {
    expect(lastBucket(364)).toBe(LAST[0])
    expect(lastBucket(365)).toBe(LAST[1])
    expect(lastBucket(730)).toBe(LAST[1])
    expect(lastBucket(731)).toBe(LAST[2])
    expect(lastBucket(null)).toBe(LAST[3])
  })
})
