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
  const dialog = await screen.findByRole("dialog")
  expect(screen.getByRole("radio", { name: "Daily" }).getAttribute("aria-checked")).toBe("true")
  expect(screen.queryByRole("radiogroup", { name: "Day" })).toBeNull()
  fireEvent.click(screen.getByRole("radio", { name: "Weekly" }))
  fireEvent.click(screen.getByRole("radio", { name: "Friday" }))
  fireEvent.change(screen.getByLabelText("Hour"), { target: { value: "9" } })
  fireEvent.change(screen.getByLabelText("Minute"), { target: { value: "30" } })
  expect(screen.getByRole("radio", { name: "AM" }).getAttribute("aria-checked")).toBe("true")
  fireEvent.click(screen.getByRole("button", { name: "Create" }))
  await waitFor(() => expect(dialog.isConnected).toBe(false))
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

it("edits the time as hour, minute and half of the day; junk in a cell leaves the time alone", async () => {
  const fetch = vi.fn((_url: string, options?: RequestInit) =>
    Promise.resolve(
      new Response(
        JSON.stringify(
          options?.method === "POST"
            ? { ...task, installs: ["codex"], codex_install_status: "installed" }
            : {
                ...task,
                installs: ["codex"],
                codex_install_status: "installed",
                codex_thread_url: "codex://t",
              },
        ),
      ),
    ),
  )
  vi.stubGlobal("fetch", fetch)
  mount()
  fireEvent.click(await screen.findByRole("button", { name: "Edit schedule" }))
  expect(await screen.findByRole("dialog", { name: "Edit schedule" })).toBeTruthy()
  const hour = screen.getByLabelText<HTMLInputElement>("Hour")
  const minute = screen.getByLabelText<HTMLInputElement>("Minute")
  expect(hour.value).toBe("6")
  expect(minute.value).toBe("00")
  fireEvent.change(hour, { target: { value: "13" } })
  fireEvent.blur(hour)
  expect(hour.value).toBe("6")
  fireEvent.keyDown(minute, { key: "ArrowDown" })
  expect(minute.value).toBe("59")
  fireEvent.click(screen.getByRole("radio", { name: "PM" }))
  fireEvent.click(screen.getByRole("button", { name: "Save" }))
  await waitFor(() => expect(fetch.mock.calls.some(([, options]) => options?.method === "POST")).toBe(true))
  const body = fetch.mock.calls.find(([, options]) => options?.method === "POST")?.[1]?.body
  if (!(body instanceof URLSearchParams)) throw new Error("Missing schedule request")
  expect(body.get("time")).toBe("18:59")
  expect(body.get("cadence")).toBe("daily")
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
