import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react"
import { MemoryRouter } from "react-router-dom"
import { afterEach, expect, it, vi } from "vitest"

import { TeamPage } from "./TeamPage"

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

const NOW = Date.now()
const TEAM = {
  me: { email: "casey@powerset.co" },
  members: [
    {
      operator_id: "op-1",
      name: "Casey Bravo",
      email: "casey@powerset.co",
      last_seen_at: new Date(NOW - 10_000).toISOString(),
    },
    { operator_id: "op-2", name: "", email: "quinn@powerset.co", last_seen_at: "" },
  ],
}
const THREAD = [
  {
    id: "m1",
    direction: "out",
    text: "Why did refresh signals fail?",
    at: new Date(NOW - 60_000).toISOString(),
  },
  {
    id: "m2",
    direction: "in",
    text: "The check ran out of memory. **Fixed** the index; restart requested.",
    at: new Date(NOW - 30_000).toISOString(),
  },
]

function serve(sent: unknown[]) {
  return vi.fn((url: string, init?: RequestInit) => {
    if (url === "/api/team") return Promise.resolve(new Response(JSON.stringify(TEAM)))
    if (url.startsWith("/api/team/thread?"))
      return Promise.resolve(new Response(JSON.stringify({ messages: THREAD })))
    if (url === "/api/team/ask") {
      const body: unknown = JSON.parse(typeof init?.body === "string" ? init.body : "{}")
      sent.push(body)
      const text = typeof body === "object" && body !== null && "text" in body ? String(body.text) : ""
      const added = { id: "m3", direction: "out", text, at: new Date(NOW).toISOString() }
      return Promise.resolve(new Response(JSON.stringify({ messages: [...THREAD, added] })))
    }
    return Promise.resolve(new Response("not found", { status: 404 }))
  })
}

function mount(path = "/team") {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[path]}>
        <TeamPage />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

it("lists teammates with their agents' presence and opens a thread on pick", async () => {
  const sent: unknown[] = []
  vi.stubGlobal("fetch", serve(sent))
  Element.prototype.scrollIntoView = vi.fn()
  mount()
  const list = await screen.findByRole("list", { name: "Teammates" })
  expect(await within(list).findByText("Casey Bravo")).toBeTruthy()
  expect(within(list).getByLabelText("Agent up")).toBeTruthy()
  expect(within(list).getByText("you")).toBeTruthy()
  expect(within(list).getAllByText(/quinn@powerset.co/).length).toBeGreaterThan(0)
  expect(within(list).getByText(/casey@powerset.co/)).toBeTruthy()
  expect(within(list).getByLabelText("Away")).toBeTruthy()
  expect(screen.getByText("Pick a teammate.")).toBeTruthy()
  fireEvent.click(within(list).getByRole("button", { name: /Casey Bravo/ }))
  const thread = await screen.findByRole("region", { name: "Thread with Casey Bravo's agent" })
  await waitFor(() => expect(within(thread).getByText("Why did refresh signals fail?")).toBeTruthy())
  expect(within(thread).getByText("Fixed")).toBeTruthy()
  fireEvent.change(within(thread).getByRole("textbox"), { target: { value: "Restart and check again" } })
  fireEvent.keyDown(within(thread).getByRole("textbox"), { key: "Enter" })
  await waitFor(() => expect(within(thread).getByText("Restart and check again")).toBeTruthy())
  expect(within(thread).getByRole("status").textContent).toContain("Casey Bravo's agent is working on it")
  expect(sent).toEqual([{ operator_id: "op-1", text: "Restart and check again" }])
  expect(within(thread).getByRole<HTMLTextAreaElement>("textbox").value).toBe("")
})
