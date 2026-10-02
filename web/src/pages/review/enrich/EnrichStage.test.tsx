import { cleanup, render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it } from "vitest"

import { NOTE, TITLE } from "./copy"
import { EnrichStage } from "./EnrichStage"

afterEach(cleanup)

describe("EnrichStage", () => {
  it("only waits: its moving shape, what is happening, and nothing to press", () => {
    const { container } = render(<EnrichStage />)
    expect(screen.getByRole("heading", { name: TITLE })).toBeTruthy()
    expect(screen.getByText(NOTE)).toBeTruthy()
    expect(container.querySelector(".enrich-shape")).toBeTruthy()
    expect(screen.queryByRole("button")).toBeNull()
  })
})
