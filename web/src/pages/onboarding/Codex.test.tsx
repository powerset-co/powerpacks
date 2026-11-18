import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import { MemoryRouter } from "react-router-dom"
import { afterEach, expect, it, vi } from "vitest"

import type { CodexAccountState } from "@/lib/agent/useCodexAccount"

import { Codex } from "./Codex"

let state: CodexAccountState
vi.mock("@/lib/agent/useCodexAccount", () => ({ useCodexAccount: () => state }))
vi.mock("@/lib/desktop", () => ({
  invoke: () => Promise.resolve(null),
  isDesktop: () => true,
}))

afterEach(cleanup)

function mount(overrides: Partial<CodexAccountState>) {
  state = {
    status: { installed: true, account: null },
    loading: false,
    login: null,
    error: null,
    connect: vi.fn(),
    openBrowser: vi.fn(),
    cancel: vi.fn(),
    signOut: vi.fn(),
    retry: vi.fn(),
    ...overrides,
  }
  render(
    <QueryClientProvider client={new QueryClient()}>
      <MemoryRouter>
        <Codex />
      </MemoryRouter>
    </QueryClientProvider>,
  )
  return state
}

it("signed out: one line and one button, no status labels", () => {
  const codex = mount({})
  expect(screen.getByText("The chat runs on your ChatGPT plan.")).toBeTruthy()
  fireEvent.click(screen.getByRole("button", { name: "Connect ChatGPT" }))
  expect(codex.connect).toHaveBeenCalledOnce()
  expect(screen.queryByText("Not signed in")).toBeNull()
  expect(screen.queryByText("Not connected")).toBeNull()
  expect(screen.queryByRole("button", { name: "Continue" })).toBeNull()
})

it("signed in: says who, offers Continue and Sign out", () => {
  const codex = mount({
    status: { installed: true, account: { kind: "chatgpt", email: "casey@example.com", plan: "plus" } },
  })
  expect(screen.getByText("Signed in as casey@example.com.")).toBeTruthy()
  expect(screen.getByRole("button", { name: "Continue" })).toBeTruthy()
  expect(screen.queryByRole("button", { name: "Connect ChatGPT" })).toBeNull()
  fireEvent.click(screen.getByRole("button", { name: "Sign out" }))
  expect(codex.signOut).toHaveBeenCalledOnce()
})

it("Codex did not answer: says so and offers Try again", () => {
  const codex = mount({ status: undefined, error: "Codex did not answer." })
  expect(screen.getByText("Could not connect to Codex.")).toBeTruthy()
  fireEvent.click(screen.getByRole("button", { name: "Try again" }))
  expect(codex.retry).toHaveBeenCalledOnce()
})
