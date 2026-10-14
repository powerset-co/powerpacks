import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { afterEach, expect, it, vi } from "vitest"

import { must } from "@/lib/must"

import { SidebarFooter } from "./SidebarFooter"

const auth = vi.hoisted(() => ({ fetchAccount: vi.fn(), signIn: vi.fn() }))
vi.mock("@/lib/api/account", () => auth)
vi.mock("@/lib/api/feedback", () => ({ signIn: auth.signIn }))
vi.mock("@/lib/desktop", () => ({ isDesktop: () => true }))
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
  auth.fetchAccount.mockResolvedValue({ signed_in: false, email: null, name: null, picture: null })
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
  auth.fetchAccount.mockResolvedValue({
    signed_in: true,
    email: "casey@example.com",
    name: "Casey",
    picture: null,
  })
  auth.signIn.mockResolvedValue(undefined)
  mount(rail)
  fireEvent.click(await screen.findByRole("button", { name: "Reconnect Powerset" }))
  await waitFor(() => expect(auth.signIn).toHaveBeenCalledOnce())
})

it("shows who is signed in by picture, name and email", async () => {
  auth.fetchAccount.mockResolvedValue({
    signed_in: true,
    email: "casey@example.com",
    name: "Casey Bravo",
    picture: "https://pictures.example.test/casey.jpg",
  })
  mount()
  expect(await screen.findByText("Casey Bravo")).toBeTruthy()
  expect(screen.getByText("casey@example.com")).toBeTruthy()
  const picture = must(document.querySelector("img"), "the picture")
  expect(picture.getAttribute("src")).toBe("https://pictures.example.test/casey.jpg")
  fireEvent.error(picture)
  expect(document.querySelector("img")).toBeNull()
  expect(screen.getByText("CB")).toBeTruthy()
})

it("names a login that stored no name by its email", async () => {
  auth.fetchAccount.mockResolvedValue({
    signed_in: true,
    email: "casey@example.com",
    name: null,
    picture: null,
  })
  mount()
  expect(await screen.findByText("casey@example.com")).toBeTruthy()
  expect(document.querySelector("img")).toBeNull()
})
