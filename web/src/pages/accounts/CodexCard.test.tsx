import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import { afterEach, expect, it, vi } from "vitest"

import type { CodexAccountState } from "@/lib/agent/useCodexAccount"

import { CodexCard } from "./CodexCard"

const account = vi.hoisted(() => ({ useCodexAccount: vi.fn() }))
vi.mock("@/lib/agent/useCodexAccount", () => account)
afterEach(cleanup)

function mount(overrides: Partial<CodexAccountState>) {
  const state: CodexAccountState = {
    status: undefined,
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
  account.useCodexAccount.mockReturnValue(state)
  render(<CodexCard />)
  return state
}

it("does not call Codex missing while checking its account", () => {
  mount({ loading: true })
  expect(screen.getByText("Checking Codex…")).toBeTruthy()
  expect(screen.queryByText("Missing from this app")).toBeNull()
})

it("offers a retry when Codex fails to answer", () => {
  const state = mount({ error: "Codex did not answer." })
  fireEvent.click(screen.getByRole("button", { name: "Try again" }))
  expect(state.retry).toHaveBeenCalledOnce()
  expect(screen.getByText("Codex did not answer.")).toBeTruthy()
  expect(screen.queryByText("Missing from this app")).toBeNull()
})

it("identifies API billing instead of claiming the ChatGPT plan covers it", () => {
  mount({ status: { installed: true, account: { kind: "apiKey" } } })
  expect(screen.getByText("Uses your API key. API usage is billed separately.")).toBeTruthy()
  expect(screen.queryByText("Runs Chat on your ChatGPT plan.")).toBeNull()
  expect(screen.queryByText("Plan")).toBeNull()
})

it("shows the plan as a badge beside the account, with the Pro tiers named", () => {
  const signedIn = (plan: string) => ({
    status: { installed: true, account: { kind: "chatgpt" as const, email: "someone@example.com", plan } },
  })
  mount(signedIn("prolite"))
  expect(screen.getByText("Pro 100")).toBeTruthy()
  expect(screen.getByText("someone@example.com")).toBeTruthy()
  expect(screen.queryByText("Plan")).toBeNull()
  cleanup()
  mount(signedIn("pro"))
  expect(screen.getByText("Pro 200")).toBeTruthy()
  cleanup()
  mount(signedIn("plus"))
  expect(screen.getByText("Plus")).toBeTruthy()
})
