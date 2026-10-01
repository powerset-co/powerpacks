import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"

import { queuePosition } from "@/testing/review-fixture"

import { CarouselNav } from "./CarouselNav"

afterEach(cleanup)

describe("CarouselNav", () => {
  it("asks for the neighbouring positions, wrapping at the ends", () => {
    const onIndex = vi.fn()
    render(<CarouselNav queue={queuePosition({ index: 0, total: 3 })} onIndex={onIndex} />)
    fireEvent.click(screen.getByRole("button", { name: "Previous" }))
    fireEvent.click(screen.getByRole("button", { name: "Next" }))
    expect(onIndex.mock.calls).toEqual([[2], [1]])
  })
})
