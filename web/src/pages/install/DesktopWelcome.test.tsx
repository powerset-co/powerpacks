import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { MemoryRouter, Route, Routes } from "react-router-dom"
import { afterEach, expect, it, vi } from "vitest"

import { DesktopWelcome } from "./DesktopWelcome"

vi.mock("@/lib/agent/useCodexAccount", () => ({
  useCodexAccount: () => ({
    status: { installed: true, account: null },
    loading: false,
    login: null,
    error: null,
    connect: vi.fn(),
  }),
}))

function reply(body: unknown) {
  return Promise.resolve(new Response(JSON.stringify(body)))
}

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

it("lists the import's steps as the app reports them, then Connect for what did not come over", async () => {
  let finish!: () => void
  const imported = new Promise<void>((resolve) => {
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
  // What the imported install brings: Powerset and Gmail signed in.
  const fetch = vi.fn((url: string) =>
    url === "/api/account"
      ? reply({ signed_in: true, email: "casey@example.com", name: null, picture: null })
      : url === "/accounts/api/signed-in"
        ? reply({
            linkedin: false,
            gmail: true,
            imessage: false,
            whatsapp: false,
            mailboxes: ["casey@example.com"],
          })
        : url === "/api/install/connect"
          ? reply({
              step: "runtime",
              status: "waiting",
              installer_pid: 0,
              message: "",
              steps: {},
              action: null,
            })
          : reply({
              browser: { ok: true, name: "Chrome", install: null },
            }),
  )
  vi.stubGlobal("fetch", fetch)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  client.setQueryData(["people"], [])
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={["/install"]}>
        <Routes>
          <Route path="/install" element={<DesktopWelcome />} />
          <Route path="/people" element={<p>People page</p>} />
        </Routes>
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
  // The local page is down while the data moves: nothing asks it anything.
  expect(fetch).not.toHaveBeenCalled()
  await act(async () => {
    step?.({ payload: 4 })
    finish()
    await imported
  })
  await waitFor(() => expect(client.getQueryState(["people"])?.isInvalidated).toBe(true))
  expect(await screen.findByText("Your data is here. Connect anything that didn't come over.")).toBeTruthy()
  const done = await screen.findByRole("button", { name: "Done" })
  expect(screen.queryByRole("button", { name: "Back" })).toBeNull()
  fireEvent.click(done)
  expect(await screen.findByText("People page")).toBeTruthy()
  expect(invoke).not.toHaveBeenCalledWith("onboard_continue", expect.anything())
})
