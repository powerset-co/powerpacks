import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { MemoryRouter } from "react-router-dom"
import { afterEach, expect, it, vi } from "vitest"

import { DesktopWelcome } from "./DesktopWelcome"

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

it("imports an existing network without requiring tools for a new setup", async () => {
  const invoke = vi.fn((command: string) =>
    Promise.resolve(command === "setup_import_source" ? "/Users/casey/powerpacks" : true),
  )
  vi.stubGlobal("__TAURI__", { core: { invoke } })
  const fetch = vi.fn(() =>
    Promise.resolve(
      new Response(
        JSON.stringify({
          browser: { ok: false, name: null, install: null },
          gcloud: { ok: false, install: null },
        }),
      ),
    ),
  )
  vi.stubGlobal("fetch", fetch)
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter>
        <DesktopWelcome />
      </MemoryRouter>
    </QueryClientProvider>,
  )
  const button = await screen.findByRole("button", { name: /Import existing data/ })
  await waitFor(() => expect(button.hasAttribute("disabled")).toBe(false))
  fireEvent.click(button)
  await waitFor(() => expect(invoke).toHaveBeenCalledWith("setup_import", undefined))
  expect(fetch).not.toHaveBeenCalled()
  expect(invoke).not.toHaveBeenCalledWith("onboard_continue", expect.anything())
})
