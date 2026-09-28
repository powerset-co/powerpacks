import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import { useState } from "react"
import { afterEach, describe, expect, it, vi } from "vitest"

import { DetailsSection } from "./DetailsSection"

afterEach(cleanup)

function Harness({ onToggle }: { onToggle: (open: boolean) => void }) {
  const [open, setOpen] = useState(false)
  return (
    <DetailsSection
      sectionKey="facts"
      title="Facts"
      count={3}
      open={open}
      onToggle={(next) => {
        onToggle(next)
        setOpen(next)
      }}
    >
      <p>Jordan Bravo works at Example Co.</p>
    </DetailsSection>
  )
}

describe("DetailsSection", () => {
  it("reports the new open state and follows it; the shut body stays mounted, folded", () => {
    const onToggle = vi.fn()
    const { container } = render(<Harness onToggle={onToggle} />)
    const toggle = screen.getByRole("button", { name: /Facts/ })
    const section = container.querySelector("[data-section='facts']")
    expect(toggle.getAttribute("aria-expanded")).toBe("false")
    expect(screen.getByText("3").tagName).toBe("SMALL")
    expect(
      screen.getByText("Jordan Bravo works at Example Co.").closest("[data-open='false']"),
    ).not.toBeNull()

    fireEvent.click(toggle)
    expect(onToggle).toHaveBeenLastCalledWith(true)
    expect(toggle.getAttribute("aria-expanded")).toBe("true")
    expect(section?.getAttribute("data-open")).toBe("true")

    fireEvent.click(toggle)
    expect(onToggle).toHaveBeenLastCalledWith(false)
    expect(onToggle).toHaveBeenCalledTimes(2)
  })
})
