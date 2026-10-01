import { describe, expect, it } from "vitest"

import { carouselIndex } from "./queue"

describe("carouselIndex", () => {
  it("steps and wraps both ways", () => {
    expect(carouselIndex({ index: 0, total: 3 }, "next")).toBe(1)
    expect(carouselIndex({ index: 2, total: 3 }, "next")).toBe(0)
    expect(carouselIndex({ index: 0, total: 3 }, "previous")).toBe(2)
    expect(carouselIndex({ index: 0, total: 0 }, "previous")).toBe(0)
  })
})
