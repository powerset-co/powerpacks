import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"

import { Clamp } from "./Clamp"

// jsdom has no layout: each case says how tall the text is against its three-line box.
function sizes(scrollHeight: number, clientHeight: number) {
  vi.spyOn(HTMLElement.prototype, "scrollHeight", "get").mockReturnValue(scrollHeight)
  vi.spyOn(HTMLElement.prototype, "clientHeight", "get").mockReturnValue(clientHeight)
}

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe("Clamp", () => {
  it("offers Show more only when the text runs past three lines, and opens it", () => {
    sizes(120, 60)
    render(<Clamp text="A long story" />)
    const more = screen.getByRole("button", { name: "Show more" })
    expect(more.getAttribute("aria-expanded")).toBe("false")
    expect(screen.getByText("A long story").getAttribute("data-clamped")).toBe("true")
    fireEvent.click(more)
    expect(screen.getByRole("button", { name: "Show less" }).getAttribute("aria-expanded")).toBe("true")
    expect(screen.getByText("A long story").getAttribute("data-clamped")).toBe("false")
  })

  it("shows no button for text that fits", () => {
    sizes(40, 60)
    render(<Clamp text="Short" />)
    expect(screen.queryByRole("button")).toBeNull()
  })
})
