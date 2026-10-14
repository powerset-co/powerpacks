import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { afterEach, expect, it, vi } from "vitest"

import { invoke } from "@/lib/desktop"
import { AutoReply } from "./AutoReply"

vi.mock("@/lib/desktop", () => ({ invoke: vi.fn() }))
afterEach(() => {
  cleanup()
  vi.resetAllMocks()
})

function setup() {
  return render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <AutoReply />
    </QueryClientProvider>,
  )
}

it("reads the saved preference and only changes it on the recipient's click", async () => {
  vi.mocked(invoke).mockResolvedValueOnce(false).mockResolvedValueOnce(true).mockResolvedValueOnce(false)
  setup()
  const toggle = screen.getByRole("button", { name: "Auto Reply" })
  await waitFor(() => expect(toggle.hasAttribute("disabled")).toBe(false))
  expect(toggle.getAttribute("aria-pressed")).toBe("false")
  expect(invoke).toHaveBeenCalledExactlyOnceWith("codex_auto_reply")
  fireEvent.click(toggle)
  await waitFor(() => expect(toggle.getAttribute("aria-pressed")).toBe("true"))
  expect(invoke).toHaveBeenLastCalledWith("codex_auto_reply", { enabled: true })
  fireEvent.click(toggle)
  await waitFor(() => expect(toggle.getAttribute("aria-pressed")).toBe("false"))
  expect(invoke).toHaveBeenLastCalledWith("codex_auto_reply", { enabled: false })
})

it("keeps the prior setting visible when saving fails", async () => {
  vi.mocked(invoke).mockResolvedValueOnce(false).mockRejectedValueOnce(new Error("Could not save"))
  setup()
  const toggle = screen.getByRole("button")
  await waitFor(() => expect(toggle.hasAttribute("disabled")).toBe(false))
  fireEvent.click(toggle)
  expect(await screen.findByRole("alert")).toHaveProperty("textContent", "Could not save")
  expect(toggle.getAttribute("aria-pressed")).toBe("false")
})

it("restores the global setting when opening another search", async () => {
  vi.mocked(invoke).mockResolvedValue(true)
  const first = setup()
  await waitFor(() =>
    expect(screen.getByRole("button", { name: "Auto Reply" }).getAttribute("aria-pressed")).toBe("true"),
  )
  first.unmount()
  setup()
  await waitFor(() =>
    expect(screen.getByRole("button", { name: "Auto Reply" }).getAttribute("aria-pressed")).toBe("true"),
  )
  expect(screen.getByRole("tooltip").textContent).toContain("across all chats and searches")
})
