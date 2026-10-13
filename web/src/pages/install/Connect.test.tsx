import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react"
import { afterEach, beforeEach, expect, it, vi } from "vitest"

import { must } from "@/lib/must"

import { Connect } from "./Connect"

vi.mock("@/lib/agent/useCodexAccount", () => ({
  useCodexAccount: () => ({
    status: { installed: true, account: null },
    loading: false,
    login: null,
    error: null,
    connect: vi.fn(),
  }),
}))

const SIGNED_IN = { signed_in: true, email: "casey@example.com", name: null, picture: null }
const READY = { browser: { ok: true, name: "Chrome", install: null }, gcloud: { ok: true, install: null } }
// A connect record as GET /api/install/connect returns it: nothing run yet.
const NOTHING = {
  step: "runtime",
  status: "waiting",
  installer_pid: 0,
  message: "",
  steps: {},
  action: null,
  mailboxes: [],
}

let state: { account: object; connect: object; posts: { url: string; body: unknown }[] }
let invoke: ReturnType<typeof vi.fn>

function reply(body: unknown, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(body), { status }))
}

beforeEach(() => {
  state = { account: SIGNED_IN, connect: NOTHING, posts: [] }
  vi.stubGlobal(
    "fetch",
    vi.fn((url: string, init?: RequestInit) => {
      if (init?.method === "POST") {
        state.posts.push({ url, body: JSON.parse(typeof init.body === "string" ? init.body : "null") })
        return reply({ status: "started" }, 202)
      }
      if (url === "/api/account") return reply(state.account)
      if (url === "/api/install/connect") return reply(state.connect)
      if (url === "/api/install/preflight") return reply(READY)
      return reply({ error: url }, 404)
    }),
  )
  invoke = vi.fn((command: string) => Promise.resolve(command === "permission_messages" ? false : null))
  vi.stubGlobal("__TAURI__", { core: { invoke } })
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <Connect onBack={vi.fn()} />
    </QueryClientProvider>,
  )
}

function row(title: string): HTMLElement {
  return must(screen.getByText(title).closest("li"), title)
}

it("lists every sign-in once, in setup's order", async () => {
  mount()
  await screen.findByText("casey@example.com")
  // Each row's first line is its title (Row: the icon, then the title over the purpose).
  const titles = [...document.querySelectorAll("li")].map(
    (item) => item.querySelector("div > span")?.textContent,
  )
  expect(titles).toEqual(["Powerset", "Codex", "Messages", "LinkedIn", "Gmail", "WhatsApp"])
})

it("starts setup once Powerset is signed in and LinkedIn is connected", async () => {
  state.account = { signed_in: false, email: null, name: null, picture: null }
  state.connect = {
    ...NOTHING,
    step: "sources",
    status: "completed",
    steps: { linkedin_login: { status: "completed" } },
  }
  const { unmount } = mount()
  const setUp = await screen.findByRole("button", { name: "Set up" })
  await within(row("Powerset")).findByRole("button", { name: "Sign in" })
  expect(setUp.hasAttribute("disabled")).toBe(true)
  unmount()
  state.account = SIGNED_IN
  mount()
  const ready = await screen.findByRole("button", { name: "Set up" })
  await waitFor(() => expect(ready.hasAttribute("disabled")).toBe(false))
  fireEvent.click(ready)
  await waitFor(() => expect(invoke).toHaveBeenCalledWith("onboard_continue", { answer: {} }))
})

it("without LinkedIn's sign-in, setup starts from the profile link", async () => {
  mount()
  const setUp = await screen.findByRole("button", { name: "Set up" })
  expect(await screen.findByText("Sign in to LinkedIn or add your profile link.")).toBeTruthy()
  expect(setUp.hasAttribute("disabled")).toBe(true)
  fireEvent.click(within(row("LinkedIn")).getByRole("button", { name: "Add your profile link" }))
  const field = within(row("LinkedIn")).getByRole("textbox", { name: "LinkedIn profile link" })
  fireEvent.change(field, { target: { value: "https://www.linkedin.com/in/casey" } })
  fireEvent.submit(field)
  await waitFor(() => expect(setUp.hasAttribute("disabled")).toBe(false))
  fireEvent.click(setUp)
  await waitFor(() =>
    expect(invoke).toHaveBeenCalledWith("onboard_continue", {
      answer: { linkedinUrl: "https://www.linkedin.com/in/casey" },
    }),
  )
})

it("connects a source and follows its run to the check", async () => {
  mount()
  fireEvent.click(await within(row("LinkedIn")).findByRole("button", { name: "Sign in" }))
  await waitFor(() =>
    expect(state.posts).toContainEqual({ url: "/api/install/connect", body: { source: "linkedin" } }),
  )
  state.connect = {
    ...NOTHING,
    step: "linkedin_login",
    status: "running",
    installer_pid: 41,
    message: "Checking LinkedIn",
  }
  expect(await within(row("LinkedIn")).findByText("Checking LinkedIn", {}, { timeout: 4_000 })).toBeTruthy()
  expect(screen.getByRole("button", { name: "Set up" }).hasAttribute("disabled")).toBe(true)
  state.connect = {
    ...NOTHING,
    step: "sources",
    status: "completed",
    steps: { linkedin_login: { status: "completed" } },
  }
  expect(await screen.findByRole("img", { name: "LinkedIn connected" }, { timeout: 4_000 })).toBeTruthy()
})

it("shows a stopped wait's line and command with Try again", async () => {
  state.connect = {
    ...NOTHING,
    step: "gmail_tools",
    status: "waiting",
    message: "Your Mac password is needed to prepare import tools.",
    action: { kind: "error", command: "brew install msgvault" },
  }
  mount()
  const gmail = row("Gmail")
  expect(await within(gmail).findByText("brew install msgvault")).toBeTruthy()
  expect(within(gmail).getByText("Your Mac password is needed to prepare import tools.")).toBeTruthy()
  expect(within(gmail).getByRole("button", { name: "Try again" })).toBeTruthy()
})

it("shows WhatsApp's code while it waits on a scan", async () => {
  state.connect = {
    ...NOTHING,
    step: "whatsapp_login",
    status: "waiting",
    installer_pid: 42,
    message: "Connect WhatsApp",
    action: { kind: "qr", qr_url: "/api/install/qr?connect=1&t=1" },
  }
  mount()
  const code = await screen.findByRole("img", { name: "WhatsApp code to scan" })
  expect(code.getAttribute("src")).toBe("/api/install/qr?connect=1&t=1")
})

it("connects Gmail with the Powerset address, then adds another mailbox after the first", async () => {
  mount()
  fireEvent.click(await within(row("Gmail")).findByRole("button", { name: "Connect" }))
  await waitFor(() =>
    expect(state.posts).toContainEqual({
      url: "/api/install/connect",
      body: { source: "gmail", emails: ["casey@example.com"] },
    }),
  )
  cleanup()
  state.connect = {
    ...NOTHING,
    step: "sources",
    status: "completed",
    steps: { gmail_login: { status: "completed" } },
    mailboxes: ["casey@example.com"],
  }
  mount()
  const gmail = row("Gmail")
  fireEvent.click(await within(gmail).findByRole("button", { name: "Add another" }))
  const field = within(gmail).getByRole("textbox", { name: "Gmail address" })
  fireEvent.change(field, { target: { value: "work@example.com" } })
  fireEvent.submit(field)
  await waitFor(() =>
    expect(state.posts).toContainEqual({
      url: "/api/install/connect",
      body: { source: "gmail", emails: ["casey@example.com", "work@example.com"] },
    }),
  )
})
