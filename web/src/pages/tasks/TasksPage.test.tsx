import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { afterEach, expect, it, vi } from "vitest"

import { TasksPage } from "./TasksPage"

const task = {
  id: "synthetic-refresh",
  name: "Refresh message sources",
  schedule: "Daily at 6:00 AM",
  command: "synthetic",
  installs: [],
  runs: [],
  schedule_settings: { cadence: "daily", time: "06:00", day: "MO", timezone: "America/Los_Angeles" },
  codex_thread_url: null,
  codex_install_status: "not_installed",
}
function mount() {
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <TasksPage />
    </QueryClientProvider>,
  )
}
afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

it.each(["codex", "claude"])("%s opens the modal before submitting its selected schedule", async (runner) => {
  const fetch = vi.fn((_url: string, options?: RequestInit) =>
    Promise.resolve(
      new Response(
        JSON.stringify(
          options?.method === "POST"
            ? {
                ...task,
                installs: [runner],
                codex_install_status: runner === "codex" ? "installed" : "not_installed",
                codex_thread_url: runner === "codex" ? "codex://threads/synthetic" : null,
              }
            : task,
        ),
      ),
    ),
  )
  vi.stubGlobal("fetch", fetch)
  mount()
  fireEvent.click(
    await screen.findByRole("button", { name: runner === "codex" ? "Install Codex" : "Install Claude" }),
  )
  expect(fetch.mock.calls.some(([, options]) => options?.method === "POST")).toBe(false)
  fireEvent.change(await screen.findByLabelText("Repeat"), { target: { value: "weekly" } })
  fireEvent.change(screen.getByLabelText("Day"), { target: { value: "FR" } })
  fireEvent.change(screen.getByLabelText("Time"), { target: { value: "09:30" } })
  fireEvent.click(screen.getByRole("button", { name: "Create" }))
  await waitFor(() => expect(fetch.mock.calls.some(([, options]) => options?.method === "POST")).toBe(true))
  const options = fetch.mock.calls.find(([, options]) => options?.method === "POST")?.[1]
  const body = options?.body
  if (!(body instanceof URLSearchParams)) throw new Error("Missing schedule request")
  expect(Object.fromEntries(body)).toEqual({
    runner,
    cadence: "weekly",
    time: "09:30",
    day: "FR",
    timezone: "America/Los_Angeles",
  })
  if (runner === "codex")
    expect((await screen.findByRole("link", { name: "Open chat" })).getAttribute("href")).toBe(
      "codex://threads/synthetic",
    )
  else expect(screen.queryByRole("link", { name: "Open chat" })).toBeNull()
  expect(screen.queryByText(/prefilled/)).toBeNull()
})

it("does not call a pending native import installed", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(() =>
      Promise.resolve(
        new Response(
          JSON.stringify({
            ...task,
            codex_install_status: "pending",
            codex_thread_url: "codex://threads/synthetic",
          }),
        ),
      ),
    ),
  )
  mount()
  expect((await screen.findByRole("status")).textContent).toBe(
    "Waiting for Codex to register the schedule...",
  )
  expect(screen.queryByRole("button", { name: "Save schedule" })).toBeNull()
  expect(screen.getByRole("button", { name: "Installing" }).hasAttribute("disabled")).toBe(true)
})

it("shows a creation error without offering a nonexistent chat", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn((_url: string, options?: RequestInit) =>
      Promise.resolve(
        options?.method === "POST"
          ? new Response(JSON.stringify({ error: "Codex could not persist the chat." }), { status: 500 })
          : new Response(JSON.stringify(task)),
      ),
    ),
  )
  mount()
  fireEvent.click(await screen.findByRole("button", { name: "Install Codex" }))
  fireEvent.click(screen.getByRole("button", { name: "Create" }))
  await screen.findByText("Codex could not persist the chat.")
  expect(screen.queryByRole("link", { name: "Open chat" })).toBeNull()
})
