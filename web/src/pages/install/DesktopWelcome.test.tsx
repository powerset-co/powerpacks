import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { MemoryRouter } from "react-router-dom"
import { afterEach, expect, it, vi } from "vitest"

import { DesktopWelcome } from "./DesktopWelcome"

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

it.each([true, false])("lists the import's steps as the app reports them (setup done: %s)", async (done) => {
  let finish!: (done: boolean) => void
  const imported = new Promise<boolean>((resolve) => {
    finish = resolve
  })
  const invoke = vi.fn((command: string) =>
    command === "setup_import_source"
      ? Promise.resolve("/Users/casey/powerpacks")
      : command === "setup_import"
        ? imported
        : Promise.resolve(null),
  )
  // The app's step events: the page subscribes once the import starts.
  let step: ((event: { payload: unknown }) => void) | null = null
  const listen = vi.fn((_event: string, handler: (event: { payload: unknown }) => void) => {
    step = handler
    return Promise.resolve(() => undefined)
  })
  vi.stubGlobal("__TAURI__", { core: { invoke }, event: { listen } })
  const fetch = vi.fn()
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
  expect(screen.getByRole("heading", { name: "Bringing over your data" })).toBeTruthy()
  expect(screen.getByText("0 of 4 tasks completed")).toBeTruthy()
  expect(listen).toHaveBeenCalledWith("import://step", expect.any(Function))
  act(() => step?.({ payload: 2 }))
  expect(screen.getByText("2 of 4 tasks completed")).toBeTruthy()
  await act(async () => {
    step?.({ payload: 4 })
    finish(done)
    await imported
  })
  await waitFor(() => expect(client.getQueryState(["people"])?.isInvalidated).toBe(true))
  expect(await screen.findByRole("heading", { name: "Your data is here" })).toBeTruthy()
  expect(screen.getByText("4 of 4 tasks completed")).toBeTruthy()
  const next = screen.getByRole("button", { name: done ? "Open People" : "Continue setup" })
  expect(invoke).not.toHaveBeenCalledWith("onboard_continue", expect.anything())
  if (!done) {
    fireEvent.click(next)
    await waitFor(() => expect(invoke).toHaveBeenCalledWith("onboard_continue", { answer: {} }))
  }
  expect(fetch).not.toHaveBeenCalled()
})
