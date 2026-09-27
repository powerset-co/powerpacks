import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"

import { SortHeader } from "./SortHeader"

afterEach(cleanup)

describe("SortHeader", () => {
  it.each(["ascending", "descending", "none"] as const)("puts aria-sort=%s on the cell", (sort) => {
    render(<SortHeader label="Warmth" sort={sort} onSort={vi.fn()} className="c-warmth" />)
    const cell = screen.getByRole("columnheader")
    expect(cell.getAttribute("aria-sort")).toBe(sort)
    expect(cell.className).toContain("c-warmth")
  })

  it("sorts when its labelled button is pressed", () => {
    const onSort = vi.fn()
    render(<SortHeader label="Warmth" sort="none" onSort={onSort} />)
    fireEvent.click(screen.getByRole("button", { name: "Warmth" }))
    expect(onSort).toHaveBeenCalledOnce()
  })
})
