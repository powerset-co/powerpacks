import { cleanup, render, screen } from "@testing-library/react"
import { afterEach, expect, it } from "vitest"

import { SettingsPage } from "./SettingsPage"

afterEach(cleanup)

it("has nothing to set yet", () => {
  render(<SettingsPage />)
  expect(screen.getByRole("heading", { name: "Settings" })).toBeTruthy()
  expect(screen.getByText("Nothing to set yet.")).toBeTruthy()
})
