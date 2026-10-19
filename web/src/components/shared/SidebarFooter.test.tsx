import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react"
import { MemoryRouter, Route, Routes } from "react-router-dom"
import { afterEach, beforeEach, expect, it, vi } from "vitest"

import { must } from "@/lib/must"

import { SidebarFooter } from "./SidebarFooter"

const auth = vi.hoisted(() => ({ fetchAccount: vi.fn(), signIn: vi.fn(), sendAppFeedback: vi.fn() }))
vi.mock("@/lib/api/account", () => ({ ...auth, ACCOUNT_KEY: ["account"] }))
vi.mock("@/lib/api/feedback", () => ({ signIn: auth.signIn, sendAppFeedback: auth.sendAppFeedback }))
const desktop = vi.hoisted(() => ({ invoke: vi.fn(), listen: vi.fn() }))
vi.mock("@/lib/desktop", () => ({ isDesktop: () => true, invoke: desktop.invoke, listen: desktop.listen }))
vi.mock("@/lib/api/relay", () => ({ connectRelay: vi.fn() }))
vi.mock("./RelayDot", () => ({ RelayDot: () => null }))

const SIGNED_IN = { signed_in: true, email: "casey@example.com", name: "Casey", picture: null }

function mount(rail = false) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={["/agent"]}>
        <Routes>
          <Route path="/settings" element={<p>Settings page</p>} />
          <Route path="*" element={null} />
        </Routes>
        <SidebarFooter rail={rail} />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}
beforeEach(() => {
  desktop.invoke.mockResolvedValue({ current: "3.27.0", latest: "3.27.0", available: false })
})
afterEach(() => {
  cleanup()
  vi.resetAllMocks()
  localStorage.clear()
})

/** The app with 3.28.0 out: update_install waits until the test settles it, and the download's
 *  progress events go to `progress`. */
function appWithUpdate() {
  let report: (payload: unknown) => void = () => undefined
  let done: () => void = () => undefined
  let fail: (error: string) => void = () => undefined
  desktop.listen.mockImplementation((event: string, handler: (payload: unknown) => void) => {
    if (event === "update:progress") report = handler
    return () => undefined
  })
  desktop.invoke.mockImplementation((command: string) => {
    if (command === "update_check") {
      return Promise.resolve({ current: "3.27.0", latest: "3.28.0", available: true })
    }
    if (command === "update_install") {
      return new Promise<void>((resolve, reject) => {
        done = () => resolve()
        fail = reject
      })
    }
    return Promise.resolve(null)
  })
  return {
    progress: (downloaded: number, total: number | null) => act(() => report({ downloaded, total })),
    done: () => act(() => done()),
    fail: (error: string) => act(() => fail(error)),
  }
}

function invoked(command: string): number {
  return desktop.invoke.mock.calls.filter(([name]) => name === command).length
}

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
  fireEvent.click(await screen.findByRole("button", { name: "Account" }))
  fireEvent.click(await screen.findByRole("menuitem", { name: "Reconnect Powerset" }))
  await waitFor(() => expect(auth.signIn).toHaveBeenCalledOnce())
  expect(screen.queryByRole("menu")).toBeNull()
})

it("opens Settings and sends feedback about the app from the account menu", async () => {
  auth.fetchAccount.mockResolvedValue({
    signed_in: true,
    email: "casey@example.com",
    name: "Casey",
    picture: null,
  })
  auth.sendAppFeedback.mockResolvedValue(undefined)
  mount()
  fireEvent.click(await screen.findByRole("button", { name: "Account" }))
  fireEvent.click(screen.getByRole("menuitem", { name: "Send feedback" }))
  fireEvent.change(await screen.findByRole("textbox"), { target: { value: "The picker is slow." } })
  fireEvent.click(screen.getByRole("button", { name: "Send" }))
  expect(await screen.findByText("Sent. Thank you.")).toBeTruthy()
  expect(auth.sendAppFeedback).toHaveBeenCalledWith("The picker is slow.", "/agent")
  fireEvent.click(screen.getByRole("button", { name: "Done" }))
  fireEvent.click(screen.getByRole("button", { name: "Account" }))
  fireEvent.click(screen.getByRole("menuitem", { name: "Settings" }))
  expect(await screen.findByText("Settings page")).toBeTruthy()
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

it("keeps the version out of the account menu", async () => {
  mount()
  fireEvent.click(await screen.findByRole("button", { name: "Account" }))
  expect(screen.queryByTitle("Powerpacks version")).toBeNull()
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

it("installs the update with a progress bar, then restarts into it", async () => {
  auth.fetchAccount.mockResolvedValue(SIGNED_IN)
  const app = appWithUpdate()
  mount()
  expect(await screen.findByText("v3.28.0 · from v3.27.0")).toBeTruthy()
  fireEvent.click(screen.getByRole("button", { name: "Install update" }))
  await waitFor(() => expect(invoked("update_install")).toBe(1))
  expect(screen.getByText("Downloading…")).toBeTruthy()
  expect(screen.queryByRole("button", { name: "Dismiss this update" })).toBeNull()

  const bar = screen.getByRole("progressbar", { name: "Downloading the update" })
  app.progress(42, 100)
  expect(screen.getByText("Downloading… 42%")).toBeTruthy()
  expect(bar.getAttribute("aria-valuenow")).toBe("42")
  app.progress(100, 100)
  expect(screen.getByText("Installing…")).toBeTruthy()
  expect(bar.getAttribute("aria-valuenow")).toBe("100")

  app.done()
  expect(localStorage.getItem("update.checked")).toBeNull()
  fireEvent.click(await screen.findByRole("button", { name: "Restart now" }))
  expect(invoked("update_restart")).toBe(1)
  expect(localStorage.getItem("update.checked")).toBeNull()
})

it("says Downloading without a share while the download's size is unknown", async () => {
  auth.fetchAccount.mockResolvedValue(SIGNED_IN)
  const app = appWithUpdate()
  mount()
  fireEvent.click(await screen.findByRole("button", { name: "Install update" }))
  await waitFor(() => expect(invoked("update_install")).toBe(1))
  app.progress(5_000, null)
  expect(await screen.findByText("Downloading…")).toBeTruthy()
  expect(screen.getByRole("progressbar").getAttribute("aria-valuenow")).toBeNull()
})

it("shows why an install failed and offers Install update again", async () => {
  auth.fetchAccount.mockResolvedValue(SIGNED_IN)
  const app = appWithUpdate()
  mount()
  fireEvent.click(await screen.findByRole("button", { name: "Install update" }))
  await waitFor(() => expect(invoked("update_install")).toBe(1))
  app.progress(10, 100)
  app.fail("The signature does not match.")
  expect((await screen.findByRole("alert")).textContent).toBe("The signature does not match.")
  expect(screen.queryByRole("progressbar")).toBeNull()

  fireEvent.click(screen.getByRole("button", { name: "Install update" }))
  await waitFor(() => expect(invoked("update_install")).toBe(2))
  expect(screen.getByText("Downloading…")).toBeTruthy()
  expect(screen.queryByRole("alert")).toBeNull()
})

it("Later closes the pane; the new version waits for the next launch", async () => {
  auth.fetchAccount.mockResolvedValue(SIGNED_IN)
  const app = appWithUpdate()
  mount()
  fireEvent.click(await screen.findByRole("button", { name: "Install update" }))
  await waitFor(() => expect(invoked("update_install")).toBe(1))
  app.done()
  fireEvent.click(await screen.findByRole("button", { name: "Later" }))
  expect(screen.queryByText("Update available")).toBeNull()
  expect(invoked("update_restart")).toBe(0)
})

it("installs from the rail's icon, its tip following along, and restarts on the next click", async () => {
  auth.fetchAccount.mockResolvedValue(SIGNED_IN)
  const app = appWithUpdate()
  mount(true)
  const icon = await screen.findByRole("button", { name: "Update available · v3.28.0. Install update" })
  const tip = () => within(icon).getByRole("tooltip").textContent
  fireEvent.click(icon)
  await waitFor(() => expect(invoked("update_install")).toBe(1))
  app.progress(42, 100)
  expect(tip()).toBe("Downloading… 42%")
  fireEvent.click(icon)
  await new Promise((resolve) => setTimeout(resolve, 0))
  expect(invoked("update_install")).toBe(1)

  app.done()
  await waitFor(() => expect(tip()).toBe("Restart to update"))
  expect(icon.getAttribute("aria-label")).toBe("Restart to update")
  fireEvent.click(icon)
  expect(invoked("update_restart")).toBe(1)
  expect(localStorage.getItem("update.checked")).toBeNull()
})

it("ignores an old saved check and checks the installed version on launch", async () => {
  auth.fetchAccount.mockResolvedValue(SIGNED_IN)
  localStorage.setItem(
    "update.checked",
    JSON.stringify({ at: Date.now(), update: { current: "3.26.0", latest: "3.27.0", available: true } }),
  )
  localStorage.setItem("update.dismissed", JSON.stringify("3.26.0"))
  mount()
  await waitFor(() => expect(invoked("update_check")).toBe(1))
  expect(screen.queryByText("Update available")).toBeNull()
  expect(localStorage.getItem("update.dismissed")).toBe(JSON.stringify("3.26.0"))
  cleanup()
  mount()
  await waitFor(() => expect(invoked("update_check")).toBe(2))
})
