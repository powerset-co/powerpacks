import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import { MemoryRouter, Route, Routes } from "react-router-dom"
import { afterEach, expect, it, vi } from "vitest"

import { isDesktop } from "@/lib/desktop"

import { SettingsPage } from "./SettingsPage"

vi.mock("@/lib/desktop", () => ({ isDesktop: vi.fn(() => false) }))
vi.mock("@/pages/install/Connect", () => ({
  Connect: ({ onDone }: { onDone: () => void }) => (
    <button type="button" onClick={onDone}>
      Done
    </button>
  ),
}))

function mount() {
  render(
    <MemoryRouter initialEntries={["/settings"]}>
      <Routes>
        <Route path="/settings" element={<SettingsPage />} />
        <Route path="/people" element={<p>People page</p>} />
      </Routes>
    </MemoryRouter>,
  )
}

afterEach(() => {
  cleanup()
  vi.mocked(isDesktop).mockReturnValue(false)
})

it("has nothing to set yet in a browser", () => {
  mount()
  expect(screen.getByRole("heading", { name: "Settings" })).toBeTruthy()
  expect(screen.getByText("Nothing to set yet.")).toBeTruthy()
})

it("offers Repair setup in the desktop app: the Connect list, Done going home", () => {
  vi.mocked(isDesktop).mockReturnValue(true)
  mount()
  expect(screen.getByRole("heading", { name: "Repair setup" })).toBeTruthy()
  expect(screen.queryByText("Nothing to set yet.")).toBeNull()
  fireEvent.click(screen.getByRole("button", { name: "Done" }))
  expect(screen.getByText("People page")).toBeTruthy()
})
