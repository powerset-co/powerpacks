import { cleanup, render, screen } from "@testing-library/react"
import { afterEach, expect, it, vi } from "vitest"

import { invoke, isDesktop } from "@/lib/desktop"
import { ErrorBoundary } from "./ErrorBoundary"

vi.mock("@/lib/desktop", () => ({ invoke: vi.fn(), isDesktop: vi.fn() }))
afterEach(() => {
  cleanup()
  vi.resetAllMocks()
})

function Broken(): never {
  throw new Error("boom")
}

it("shows what to do instead of a blank page, reports it, and clears on the next page", () => {
  vi.mocked(isDesktop).mockReturnValue(true)
  vi.mocked(invoke).mockResolvedValue(undefined)
  vi.spyOn(console, "error").mockImplementation(() => undefined)
  const view = render(
    <ErrorBoundary at="/broken">
      <Broken />
    </ErrorBoundary>,
  )
  expect(screen.getByText("This page hit an error. Reload to try again.")).toBeTruthy()
  expect(screen.getByRole("button", { name: "Reload" })).toBeTruthy()
  const [command, args] = vi.mocked(invoke).mock.calls[0] ?? []
  expect(command).toBe("report_crash")
  expect(args).toMatchObject({ summary: "Error: boom" })
  view.rerender(
    <ErrorBoundary at="/people">
      <p>People</p>
    </ErrorBoundary>,
  )
  expect(screen.getByText("People")).toBeTruthy()
})

it("reports nothing outside the desktop app", () => {
  vi.mocked(isDesktop).mockReturnValue(false)
  vi.spyOn(console, "error").mockImplementation(() => undefined)
  render(
    <ErrorBoundary at="/broken">
      <Broken />
    </ErrorBoundary>,
  )
  expect(screen.getByRole("button", { name: "Reload" })).toBeTruthy()
  expect(invoke).not.toHaveBeenCalled()
})
