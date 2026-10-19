import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react"
import { afterEach, beforeEach, expect, it, vi } from "vitest"

import type { SignedIn } from "@/lib/api/accounts"
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
const NOTHING = { step: "runtime", status: "waiting", installer_pid: 0, message: "", steps: {}, action: null }
// GET /accounts/api/signed-in on a fresh Mac: nothing signed in.
const NONE: SignedIn = { linkedin: false, gmail: false, imessage: false, whatsapp: false, mailboxes: [] }

let state: {
  account: object
  connect: object
  // The record a launched run writes first: the connect request answers once it is there.
  launch: object | null
  signed: SignedIn
  // Holds back the sign-in answer, and Powerset's browser sign-in, until a test lets them through.
  signedGate: Promise<void> | null
  signedAsks: number
  finishGate: Promise<void> | null
  posts: { url: string; body: unknown }[]
}
let invoke: ReturnType<typeof vi.fn>

function reply(body: unknown, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(body), { status }))
}

beforeEach(() => {
  state = {
    account: SIGNED_IN,
    connect: NOTHING,
    launch: null,
    signed: NONE,
    signedGate: null,
    signedAsks: 0,
    finishGate: null,
    posts: [],
  }
  vi.stubGlobal(
    "fetch",
    vi.fn((url: string, init?: RequestInit) => {
      if (init?.method === "POST") {
        state.posts.push({ url, body: JSON.parse(typeof init.body === "string" ? init.body : "null") })
        if (url === "/api/install/connect" && state.launch) state.connect = state.launch
        if (url === "/searches/auth/login/finish")
          return (state.finishGate ?? Promise.resolve()).then(() => reply({ ok: true }))
        return reply({ status: "started" }, 202)
      }
      if (url === "/api/account") return reply(state.account)
      if (url === "/api/install/connect") return reply(state.connect)
      if (url === "/accounts/api/signed-in") {
        state.signedAsks += 1
        return (state.signedGate ?? Promise.resolve()).then(() => reply(state.signed))
      }
      if (url === "/api/install/preflight") return reply(READY)
      return reply({ error: url }, 404)
    }),
  )
  invoke = vi.fn(() => Promise.resolve(null))
  vi.stubGlobal("__TAURI__", { core: { invoke } })
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

function mount(props: React.ComponentProps<typeof Connect> = { onBack: vi.fn() }) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <Connect {...props} />
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

it("shows a source connected only when it is signed in, never from the connect record alone", async () => {
  // The record says LinkedIn's sign-in finished, but its session is gone: Connect again.
  state.connect = {
    ...NOTHING,
    step: "sources",
    status: "completed",
    steps: { linkedin_login: { status: "completed" } },
  }
  state.signed = { ...NONE, imessage: true }
  mount()
  expect(await within(row("Messages")).findByText("Connected")).toBeTruthy()
  expect(within(row("LinkedIn")).getByRole("button", { name: "LinkedIn: Connect" })).toBeTruthy()
})

it("asks again what is signed in on coming back to the window (Full Disk Access from System Settings)", async () => {
  mount()
  const messages = row("Messages")
  await within(messages).findByRole("button", { name: "Messages: Connect" })
  state.signed = { ...NONE, imessage: true }
  fireEvent.focus(window)
  expect(await within(messages).findByText("Connected")).toBeTruthy()
})

it("starts setup once Powerset is signed in and LinkedIn is connected", async () => {
  state.account = { signed_in: false, email: null, name: null, picture: null }
  state.signed = { ...NONE, linkedin: true }
  const { unmount } = mount()
  const setUp = await screen.findByRole("button", { name: "Set up" })
  await within(row("Powerset")).findByRole("button", { name: "Powerset: Connect" })
  expect(setUp.hasAttribute("disabled")).toBe(true)
  unmount()
  state.account = SIGNED_IN
  mount()
  const ready = await screen.findByRole("button", { name: "Set up" })
  await waitFor(() => expect(ready.hasAttribute("disabled")).toBe(false))
  fireEvent.click(ready)
  await waitFor(() => expect(invoke).toHaveBeenCalledWith("onboard_continue", { answer: {} }))
})

it("with nothing connected, setup starts without a profile link", async () => {
  mount()
  const setUp = await screen.findByRole("button", { name: "Set up" })
  await waitFor(() => expect(setUp.hasAttribute("disabled")).toBe(false))
  fireEvent.click(setUp)
  await waitFor(() => expect(invoke).toHaveBeenCalledWith("onboard_continue", { answer: {} }))
})

it("after an import, ends with Done: no Back, no setup to start, no profile link", async () => {
  // Gmail came over without LinkedIn: a new setup would ask for the profile link here.
  state.signed = { ...NONE, gmail: true, mailboxes: ["casey@example.com"] }
  const onDone = vi.fn()
  mount({ onDone })
  const done = await screen.findByRole("button", { name: "Done" })
  expect(await within(row("Gmail")).findByText("Connected")).toBeTruthy()
  expect(screen.queryByRole("button", { name: "Back" })).toBeNull()
  expect(screen.queryByRole("button", { name: "Set up" })).toBeNull()
  expect(screen.queryByText("Anything left unconnected is skipped.")).toBeNull()
  expect(within(row("LinkedIn")).queryByRole("button", { name: "Add your profile link" })).toBeNull()
  fireEvent.click(done)
  expect(onDone).toHaveBeenCalledOnce()
  expect(invoke).not.toHaveBeenCalledWith("onboard_continue", expect.anything())
})

it("without LinkedIn's sign-in, setup starts from the profile link", async () => {
  state.signed = { ...NONE, whatsapp: true }
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

it("shows Working from the click until the sign-in answer after its run is in, nothing in between", async () => {
  // LinkedIn's last sign-in stopped (a closed window).
  state.connect = {
    ...NOTHING,
    step: "linkedin_login",
    status: "failed",
    message: "LinkedIn sign-in stopped.",
  }
  state.launch = {
    ...NOTHING,
    step: "linkedin_login",
    status: "running",
    installer_pid: 41,
    message: "Checking LinkedIn",
  }
  mount()
  const linkedin = row("LinkedIn")
  fireEvent.click(await within(linkedin).findByRole("button", { name: "LinkedIn: Try again" }))
  // At once, before the run has written anything: Working, and what to do.
  expect(within(linkedin).getByText("Working")).toBeTruthy()
  expect(within(linkedin).getByText("Sign in to LinkedIn in the window that opens.")).toBeTruthy()
  await waitFor(() =>
    expect(state.posts).toContainEqual({ url: "/api/install/connect", body: { source: "linkedin" } }),
  )
  expect(within(linkedin).getByText("Working")).toBeTruthy()
  // Never the run's own status lines.
  expect(within(linkedin).queryByText("Checking LinkedIn")).toBeNull()
  expect(screen.getByRole("button", { name: "Set up" }).hasAttribute("disabled")).toBe(true)
  // The run ends; the sign-in answer after it is held back: still Working, no button.
  let release!: () => void
  state.signedGate = new Promise((resolve) => {
    release = resolve
  })
  const asked = state.signedAsks
  state.signed = { ...NONE, linkedin: true }
  state.connect = { ...NOTHING, step: "sources", status: "completed" }
  await waitFor(() => expect(state.signedAsks).toBeGreaterThan(asked), { timeout: 4_000 })
  expect(within(linkedin).getByText("Working")).toBeTruthy()
  expect(within(linkedin).queryByRole("button")).toBeNull()
  release()
  expect(await within(linkedin).findByText("Connected")).toBeTruthy()
  expect(within(linkedin).queryByText("Sign in to LinkedIn in the window that opens.")).toBeNull()
})

it("gives WhatsApp's code its box from the click, and the code fills it", async () => {
  state.launch = {
    ...NOTHING,
    step: "whatsapp_tools",
    status: "running",
    installer_pid: 42,
    message: "Preparing",
  }
  mount()
  const whatsapp = row("WhatsApp")
  fireEvent.click(await within(whatsapp).findByRole("button", { name: "WhatsApp: Connect" }))
  expect(
    within(whatsapp).getByText("In WhatsApp on your phone, go to Linked devices and scan this code."),
  ).toBeTruthy()
  expect(within(whatsapp).queryByAltText("WhatsApp code to scan")).toBeNull()
  await waitFor(() =>
    expect(state.posts).toContainEqual({ url: "/api/install/connect", body: { source: "whatsapp" } }),
  )
  state.connect = {
    ...NOTHING,
    step: "whatsapp_login",
    status: "waiting",
    installer_pid: 42,
    message: "Connect WhatsApp",
    action: { kind: "qr", qr_url: "/api/install/qr?connect=1&t=1" },
  }
  const code = await within(whatsapp).findByAltText("WhatsApp code to scan", {}, { timeout: 4_000 })
  expect(code.getAttribute("src")).toBe("/api/install/qr?connect=1&t=1")
  expect(within(whatsapp).queryByText("Connect WhatsApp")).toBeNull()
  expect(within(whatsapp).queryByText("Preparing")).toBeNull()
})

it("shows Powerset Working while you sign in in the browser, then Connected", async () => {
  state.account = { signed_in: false, email: null, name: null, picture: null }
  let finish!: () => void
  state.finishGate = new Promise((resolve) => {
    finish = resolve
  })
  mount()
  const powerset = row("Powerset")
  fireEvent.click(await within(powerset).findByRole("button", { name: "Powerset: Connect" }))
  expect(within(powerset).getByText("Working")).toBeTruthy()
  expect(within(powerset).getByText("Finish signing in to Powerset in your browser.")).toBeTruthy()
  state.account = SIGNED_IN
  finish()
  expect(await within(powerset).findByText("Connected")).toBeTruthy()
  expect(within(powerset).queryByText("Finish signing in to Powerset in your browser.")).toBeNull()
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
  expect(within(gmail).getByRole("button", { name: "Gmail: Try again" })).toBeTruthy()
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

it("connects Gmail with the Powerset address, then adds another mailbox to the signed-in ones", async () => {
  mount()
  fireEvent.click(await within(row("Gmail")).findByRole("button", { name: "Gmail: Connect" }))
  await waitFor(() =>
    expect(state.posts).toContainEqual({
      url: "/api/install/connect",
      body: { source: "gmail", emails: ["casey@example.com"] },
    }),
  )
  cleanup()
  state.signed = { ...NONE, gmail: true, mailboxes: ["casey@example.com"] }
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

it("keeps the Google app's owner first when a mailbox needs signing in again", async () => {
  state.signed = { ...NONE, mailboxes: ["owner@example.com"] }
  mount()
  const gmail = row("Gmail")
  expect(await within(gmail).findByText("owner@example.com")).toBeTruthy()
  // Signed out, Gmail is left out of setup: nothing to process, so no profile link is needed.
  const setUp = screen.getByRole("button", { name: "Set up" })
  await waitFor(() => expect(setUp.hasAttribute("disabled")).toBe(false))
  fireEvent.click(within(gmail).getByRole("button", { name: "Gmail: Connect" }))
  await waitFor(() =>
    expect(state.posts).toContainEqual({
      url: "/api/install/connect",
      body: { source: "gmail", emails: ["owner@example.com"] },
    }),
  )
})

it("shows Try again when a mailbox added to a signed-in Gmail stops, not Connected", async () => {
  state.signed = { ...NONE, gmail: true, mailboxes: ["casey@example.com"] }
  state.connect = { ...NOTHING, step: "gmail_login", status: "failed", message: "Gmail sign-in stopped." }
  mount()
  const gmail = row("Gmail")
  expect(await within(gmail).findByRole("button", { name: "Gmail: Try again" })).toBeTruthy()
  expect(within(gmail).queryByText("Connected")).toBeNull()
  expect(within(gmail).queryByText("Gmail sign-in stopped.")).toBeNull()
})

it("shows a sign-in that was under way when the screen opened once its run ends", async () => {
  state.connect = {
    ...NOTHING,
    step: "linkedin_login",
    status: "running",
    installer_pid: 41,
    message: "Checking LinkedIn",
  }
  mount()
  const linkedin = row("LinkedIn")
  expect(await within(linkedin).findByText("Working")).toBeTruthy()
  state.signed = { ...NONE, linkedin: true }
  state.connect = { ...NOTHING, step: "sources", status: "completed" }
  expect(await within(linkedin).findByText("Connected", {}, { timeout: 4_000 })).toBeTruthy()
})

it("tells you to turn on Full Disk Access, Working until you come back", async () => {
  mount()
  const messages = row("Messages")
  fireEvent.click(await within(messages).findByRole("button", { name: "Messages: Connect" }))
  expect(within(messages).getByText("Working")).toBeTruthy()
  expect(
    within(messages).getByText("Turn on Powerpacks in System Settings, then come back here."),
  ).toBeTruthy()
  state.signed = { ...NONE, imessage: true }
  fireEvent.focus(window)
  expect(await within(messages).findByText("Connected")).toBeTruthy()
  expect(
    within(messages).queryByText("Turn on Powerpacks in System Settings, then come back here."),
  ).toBeNull()
})

it("expands account details in settings without setup navigation", () => {
  mount({
    details: {
      gmail: <p>Mailbox stats</p>,
      imessage: <p>Message stats</p>,
      linkedin: <p>LinkedIn stats</p>,
      whatsapp: <p>WhatsApp stats</p>,
    },
  })
  const gmail = must(screen.getByText("Gmail").closest("details"), "Gmail details")
  expect(gmail.open).toBe(false)
  fireEvent.click(screen.getByText("Gmail"))
  expect(gmail.open).toBe(true)
  expect(screen.getByText("Mailbox stats")).toBeTruthy()
  for (const name of ["Done", "Back", "Set up"]) expect(screen.queryByRole("button", { name })).toBeNull()
})
