import { useState } from "react"
import { demoCollaboration, demoMessage } from "./fixtures.test-support"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { afterEach, beforeEach, expect, it, vi } from "vitest"
import { must } from "@/lib/must"
import { SearchHistory } from "./SearchHistory"

beforeEach(() => {
  Object.defineProperty(HTMLElement.prototype, "offsetHeight", { configurable: true, get: () => 800 })
  Object.defineProperty(HTMLElement.prototype, "offsetWidth", { configurable: true, get: () => 1200 })
  HTMLElement.prototype.scrollTo = vi.fn()
  vi.stubGlobal("matchMedia", () => ({
    matches: true,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
  }))
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {
        /* No browser layout in jsdom. */
      }
      unobserve() {
        /* No browser layout in jsdom. */
      }
      disconnect() {
        /* No browser layout in jsdom. */
      }
    },
  )
})
afterEach(() => {
  cleanup()
  sessionStorage.clear()
  vi.unstubAllGlobals()
})

function History({ privateSend }: { privateSend: (text: string) => Promise<void> }) {
  const [data, setData] = useState(demoCollaboration)
  const collaboration = {
    data,
    busy: false,
    answering: null,
    error: undefined,
    pending: [],
    loading: false,
    create: vi.fn(),
    answer: vi.fn(),
    resend: vi.fn(),
    send: (id: string, text: string, recipient_id?: string, reply_to?: string) => {
      const message = {
        ...demoMessage(text),
        recipient_id,
        reply_to,
        request_id: crypto.randomUUID(),
        status: "queued" as const,
      }
      setData((old) => ({
        ...old,
        conversations: old.conversations.map((item) =>
          item.id === id ? { ...item, messages: [...item.messages, message] } : item,
        ),
      }))
      return Promise.resolve(true)
    },
  }
  return (
    <SearchHistory
      searchId="backend-demo"
      title="Backend engineers"
      collaboration={collaboration}
      onPrivateSend={privateSend}
    />
  )
}
function setup() {
  const privateSend = vi.fn().mockResolvedValue(undefined)
  const client = new QueryClient()
  const view = render(
    <QueryClientProvider client={client}>
      <History privateSend={privateSend} />
    </QueryClientProvider>,
  )
  return { ...view, privateSend }
}
it("routes an @mention to the member and keeps follow-ups attached to its question", async () => {
  const { privateSend } = setup()
  fireEvent.change(screen.getByRole("textbox", { name: "Message" }), { target: { value: "@jor" } })
  fireEvent.click(await screen.findByRole("button", { name: "Jordan Bravo" }))
  fireEvent.change(screen.getByRole("textbox", { name: "Message" }), {
    target: { value: "Who has tuned Kubernetes?" },
  })
  fireEvent.click(screen.getByRole("button", { name: "Send" }))
  await screen.findByText("Who has tuned Kubernetes?")
  expect(privateSend).not.toHaveBeenCalled()
  fireEvent.click(must(screen.getAllByRole("button", { name: "Reply in thread" }).at(0)))
  fireEvent.change(screen.getByRole("textbox", { name: "Message" }), {
    target: { value: "Was it hands-on?" },
  })
  fireEvent.click(screen.getByRole("button", { name: "Send" }))
  await screen.findByText("Was it hands-on?")
  fireEvent.click(screen.getByRole("button", { name: "Hide 1 follow-up" }))
  await waitFor(() => expect(screen.queryByText("Was it hands-on?")).toBeNull())
  fireEvent.click(screen.getByRole("button", { name: "Show 1 follow-up" }))
  expect(screen.getByText("Was it hands-on?")).toBeTruthy()
  expect(privateSend).not.toHaveBeenCalled()
})
it("keeps the selected recipient with a draft across navigation", async () => {
  const first = setup()
  fireEvent.click(screen.getByRole("button", { name: "Mention a member" }))
  fireEvent.click(await screen.findByRole("button", { name: "Jordan Bravo" }))
  fireEvent.change(screen.getByRole("textbox", { name: "Message" }), { target: { value: "Saved question" } })
  first.unmount()
  const second = setup()
  expect(screen.getByRole<HTMLTextAreaElement>("textbox", { name: "Message" }).value).toBe("Saved question")
  fireEvent.click(screen.getByRole("button", { name: "Send" }))
  await screen.findByText("Saved question")
  expect(second.privateSend).not.toHaveBeenCalled()
})
