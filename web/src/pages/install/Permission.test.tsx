import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { afterEach, beforeEach, expect, it, vi } from "vitest"

import { fetchSignedIn } from "@/lib/api/accounts"
import { installAction } from "@/lib/api/install"
import { invoke } from "@/lib/desktop"
import { must } from "@/lib/must"
import { Permission } from "./Permission"

vi.mock("@/lib/api/accounts", () => ({ fetchSignedIn: vi.fn() }))
vi.mock("@/lib/api/install", () => ({ installAction: vi.fn() }))
vi.mock("@/lib/desktop", () => ({ invoke: vi.fn() }))

const NONE = { linkedin: false, gmail: false, imessage: false, whatsapp: false, mailboxes: [] }

beforeEach(() => {
  vi.mocked(fetchSignedIn).mockResolvedValue(NONE)
  vi.mocked(invoke).mockResolvedValue(undefined)
  vi.mocked(installAction).mockResolvedValue(undefined)
})

afterEach(() => {
  cleanup()
  vi.resetAllMocks()
})

function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <Permission />
    </QueryClientProvider>,
  )
}

it("starts a native app drag from the icon, not an HTML image drag", async () => {
  mount()
  const icon = await screen.findByRole("button", { name: "Drag Powerpacks into Full Disk Access" })
  fireEvent.mouseDown(icon, { button: 0 })
  expect(invoke).toHaveBeenCalledWith("permission_drag")
  expect(screen.getByAltText("Powerpacks").getAttribute("draggable")).toBe("false")
})

it("shows the drag icon at once, without waiting on the sign-in check", () => {
  vi.mocked(fetchSignedIn).mockReturnValue(new Promise(() => undefined))
  mount()
  expect(screen.getByRole("button", { name: "Drag Powerpacks into Full Disk Access" })).toBeTruthy()
})

it("closes automatically when polling sees permission granted", async () => {
  mount()
  await screen.findByAltText("Powerpacks")
  vi.mocked(fetchSignedIn).mockResolvedValue({ ...NONE, imessage: true })
  await waitFor(() => expect(invoke).toHaveBeenCalledWith("permission_end"), { timeout: 4000 })
})

it("reopens via the Python route then arranges Settings again", async () => {
  mount()
  fireEvent.click(screen.getByRole("button", { name: "Open the list again" }))
  await waitFor(() => expect(invoke).toHaveBeenCalledWith("permission_begin"))
  expect(installAction).toHaveBeenCalledWith("permissions")
  expect(must(vi.mocked(installAction).mock.invocationCallOrder[0], "open call")).toBeLessThan(
    must(vi.mocked(invoke).mock.invocationCallOrder[0], "begin call"),
  )
})

it("allows Skip even while the readiness check is unavailable", async () => {
  vi.mocked(fetchSignedIn).mockRejectedValue(new Error("Could not check"))
  mount()
  await screen.findByRole("alert")
  fireEvent.click(screen.getByRole("button", { name: "Skip" }))
  expect(invoke).toHaveBeenCalledWith("permission_end")
})
