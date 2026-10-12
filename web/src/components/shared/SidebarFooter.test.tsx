import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { afterEach, expect, it, vi } from "vitest"

import { SidebarFooter } from "./SidebarFooter"

const auth = vi.hoisted(() => ({ fetchAccount: vi.fn(), signIn: vi.fn() }))
vi.mock("@/lib/api/account", () => auth)
vi.mock("@/lib/api/feedback", () => ({ signIn: auth.signIn }))
vi.mock("@/lib/desktop", () => ({ isDesktop: () => true }))
vi.mock("@/lib/signin", () => ({ useSignIn: () => null }))
vi.mock("@/lib/api/update", () => ({
  lastCheck: () => null,
  dismissedVersion: () => null,
  checkUpdate: () => Promise.resolve(null),
  UPDATE_INTERVAL_MS: 60_000,
}))
vi.mock("@/lib/api/relay", () => ({ connectRelay: vi.fn() }))
vi.mock("./RelayDot", () => ({ RelayDot: () => null }))

function mount(rail = false) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <SidebarFooter rail={rail} />
    </QueryClientProvider>,
  )
}
afterEach(() => {
  cleanup()
  vi.resetAllMocks()
})

it("reports the real native login failure", async () => {
  auth.fetchAccount.mockResolvedValue({ signed_in: false, email: null, name: null })
  auth.signIn.mockRejectedValue(new Error("Sign-in did not complete. Try again."))
  mount()
  fireEvent.click(await screen.findByRole("button", { name: "Sign in" }))
  expect(await screen.findByRole("alert")).toHaveProperty(
    "textContent",
    "Sign-in did not complete. Try again.",
  )
  expect(auth.signIn).toHaveBeenCalledOnce()
})

it.each([false, true])("keeps reauthentication reachable with stored identity, rail=%s", async (rail) => {
  auth.fetchAccount.mockResolvedValue({ signed_in: true, email: "casey@example.com", name: "Casey" })
  auth.signIn.mockResolvedValue(undefined)
  mount(rail)
  fireEvent.click(await screen.findByRole("button", { name: "Reconnect Powerset" }))
  await waitFor(() => expect(auth.signIn).toHaveBeenCalledOnce())
})
