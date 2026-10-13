import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { MemoryRouter } from "react-router-dom"
import { afterEach, expect, it, vi } from "vitest"

import { DesktopWelcome } from "./DesktopWelcome"

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

it.each([true, false])("shows import progress and completion (setup done: %s)", async (done) => {
  let finish!: (done: boolean) => void
  const imported = new Promise<boolean>((resolve) => {
    finish = resolve
  })
  const invoke = vi.fn((command: string) =>
    command === "setup_import_source" ? Promise.resolve("/Users/casey/powerpacks") : imported,
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
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  client.setQueryData(["people"], [])
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <DesktopWelcome />
      </MemoryRouter>
    </QueryClientProvider>,
  )
  const button = await screen.findByRole("button", { name: /Import existing data/ })
  await waitFor(() => expect(button.hasAttribute("disabled")).toBe(false))
  fireEvent.click(button)
  await waitFor(() => expect(invoke).toHaveBeenCalledWith("setup_import", undefined))
  expect(screen.getByRole("heading", { name: "Connecting your existing data" })).toBeTruthy()
  await act(async () => {
    finish(done)
    await imported
  })
  await waitFor(() => expect(client.getQueryState(["people"])?.isInvalidated).toBe(true))
  expect(await screen.findByRole("heading", { name: "Existing data connected" })).toBeTruthy()
  expect(screen.getByRole("button", { name: done ? "Open People" : "Continue setup" })).toBeTruthy()
  expect(fetch).not.toHaveBeenCalled()
  expect(invoke).not.toHaveBeenCalledWith("onboard_continue", expect.anything())
})
