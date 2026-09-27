import { cleanup, render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it } from "vitest"

import { Chip } from "./Chip"

afterEach(cleanup)

describe("Chip", () => {
  it("reads label, value, count, then the remove mark", () => {
    render(
      <Chip pressed label="Version" count={1204} removable title="Remove">
        v3
      </Chip>,
    )
    const chip = screen.getByRole("button", { pressed: true })
    expect(chip.textContent).toBe(`Versionv3${(1204).toLocaleString()}×`)
    expect(chip.querySelector("em")?.textContent).toBe("Version")
    expect(chip.querySelector("[aria-hidden]")?.textContent).toBe("×")
  })

  it("is a bare pill by default", () => {
    render(<Chip pressed={false}>Gmail</Chip>)
    const chip = screen.getByRole("button", { pressed: false })
    expect(chip.textContent).toBe("Gmail")
    expect(chip.querySelector("em, [aria-hidden]")).toBeNull()
  })
})
