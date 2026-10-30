import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { Profiler } from "react"
import { afterEach, describe, expect, it, vi } from "vitest"

import type { InstallStatus } from "@/types/install"

import MAPPING from "../../../../packs/powerset/primitives/install/setup-steps-mapping.json"

import { InstallPage } from "./InstallPage"
import PROSE from "./prose.fixture.json"

const INSTALL: InstallStatus = {
  primitive: "powerpacks_install",
  updated_at: "2026-10-24T00:00:00Z",
  status: "running",
  step: "dependencies",
  message: "Installing what Powerpacks needs",
  log_path: "/synthetic/.powerpacks/install/install.log",
  retry_command: "bin/bootstrap --no-tools",
  plan: ["runtime", "dependencies", "skills", "account", "credentials", "connection", "network"],
  prose: PROSE,
  steps: {
    runtime: { status: "completed", message: "Mac ready" },
    dependencies: { status: "running", message: "Installing what Powerpacks needs" },
  },
}

vi.mock("@/hooks/useSetupDone", () => ({ useSetupDone: vi.fn(() => false) }))

function mount(onRender = vi.fn()) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const view = render(
    <QueryClientProvider client={client}>
      <Profiler id="install" onRender={onRender}>
        <InstallPage />
      </Profiler>
    </QueryClientProvider>,
  )
  return { client, ...view }
}

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

describe("installation progress", () => {
  it("does not claim readiness when an intermediate stage completes", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve(
          new Response(
            JSON.stringify({
              ...INSTALL,
              step: "network",
              status: "completed",
              network_name: "Personal Network",
              person_count: 328,
            }),
          ),
        ),
      ),
    )
    mount()
    await screen.findByRole("heading", { name: "Setting up Powerpacks" })
    expect(screen.queryByRole("heading", { name: "Powerpacks is ready" })).toBeNull()
  })

  const SOURCE_PLAN = [
    "linkedin_login",
    "gmail_tools",
    "gmail_login",
    "imessage_access",
    "whatsapp_tools",
    "whatsapp_login",
    "linkedin",
    "gmail_sync",
    "gmail_import",
    "imessage_import",
    "whatsapp_sync",
    "whatsapp_import",
  ]
  const LOGINS = SOURCE_PLAN.slice(0, SOURCE_PLAN.indexOf("linkedin"))

  it("shows every login as one row, then one syncing row per source", async () => {
    let status: InstallStatus = {
      ...INSTALL,
      plan: SOURCE_PLAN,
      step: "whatsapp_login",
      status: "waiting",
      steps: Object.fromEntries(
        LOGINS.slice(0, -1).map((step) => [step, { status: "completed", message: "Done" }]),
      ),
    }
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(new Response(JSON.stringify(status)))),
    )
    const { client } = mount()
    await screen.findByText("Signing you in")
    const current = () => screen.getByRole("list").querySelector('[aria-current="step"]')?.textContent
    expect(current()).toContain("Signing you inWaiting")
    expect(screen.getByRole("list").textContent).toContain("Reading your connections")

    status = {
      ...status,
      step: "linkedin",
      status: "running",
      steps: {
        ...status.steps,
        whatsapp_login: { status: "completed", message: "Done" },
        linkedin: { status: "running", message: "Reading your LinkedIn connections" },
      },
    }
    await act(() => client.invalidateQueries({ queryKey: ["install"] }))
    await waitFor(() => expect(current()).toContain("Reading your connectionsWorking"))
    expect(screen.getByText("Signing you in").closest("li")?.textContent).toContain("Done")

    for (const step of SOURCE_PLAN) {
      status = { ...status, step, status: "failed", steps: {} }
      await act(() => client.invalidateQueries({ queryKey: ["install"] }))
      await waitFor(() => {
        expect(current()).toContain("Needs a fix")
        expect(current()).toContain(MAPPING.rows.find((row) => row.steps.includes(step))?.label)
      })
    }
  })

  it("offers the matches left for review with the review page's own count", async () => {
    const status: InstallStatus = {
      ...INSTALL,
      step: "ready",
      status: "completed",
      message: "Search index ready: 403 people searchable.",
    }
    let pending = 3
    const fetch = vi.fn((url: string) =>
      Promise.resolve(
        new Response(
          JSON.stringify(
            url.startsWith("/api/review/linkedin-card")
              ? { card: null, finished: null, pending, queue: null }
              : status,
          ),
        ),
      ),
    )
    vi.stubGlobal("fetch", fetch)
    const { client } = mount()
    expect(await screen.findByText("3 LinkedIn matches need a quick look when you have time.")).toBeTruthy()
    expect(screen.getByRole("button", { name: "Review contacts" })).toBeTruthy()
    pending = 0
    await act(() => client.invalidateQueries({ queryKey: ["linkedin-left"] }))
    await waitFor(() => expect(screen.queryByRole("button", { name: "Review contacts" })).toBeNull())
  })

  it("keeps what is left to fix in view next to the review offer", async () => {
    const status: InstallStatus = {
      ...INSTALL,
      step: "ready",
      status: "completed",
      message: "Search is ready: 403 people searchable.",
      note: "Research and LinkedIn matching didn’t finish.",
    }
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) =>
        Promise.resolve(
          new Response(
            JSON.stringify(
              url.startsWith("/api/review/linkedin-card")
                ? { card: null, finished: null, pending: 2, queue: null }
                : status,
            ),
          ),
        ),
      ),
    )
    mount()
    expect(await screen.findByText("2 LinkedIn matches need a quick look when you have time.")).toBeTruthy()
    expect(screen.getByText("Research and LinkedIn matching didn’t finish.")).toBeTruthy()
  })

  it("shows no index row when every source was skipped", async () => {
    const plan = ["runtime", "dependencies", "skills", "sources", "ready"]
    const status: InstallStatus = {
      ...INSTALL,
      plan,
      step: "ready",
      status: "completed",
      message: "Powerpacks is installed",
      steps: Object.fromEntries(plan.map((step) => [step, { status: "completed", message: "Done" }])),
    }
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(new Response(JSON.stringify(status)))),
    )
    mount()
    await screen.findByText("Signing you in")
    expect(screen.queryByText("Making your connections searchable")).toBeNull()
  })

  it("shows the processing message, then Modal's words for the index, and stops on failure", async () => {
    let status: InstallStatus = { ...INSTALL, step: "enrich", message: "Enriching contacts" }
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(new Response(JSON.stringify(status)))),
    )
    const { client } = mount()
    await screen.findByText("Enriching contacts")
    status = {
      ...INSTALL,
      step: "index",
      event: "index.building",
      message: "Understanding companies",
      action: { kind: "details", details: { done: 40, total: 111, left_seconds: 90 } },
    }
    await act(() => client.invalidateQueries({ queryKey: ["install"] }))
    await screen.findByText("Understanding companies")
    // The task count stands in for any bar.
    expect(screen.queryByRole("progressbar")).toBeNull()
    status = { ...status, status: "failed", event: "step.failed", message: "Index download failed" }
    await act(() => client.invalidateQueries({ queryKey: ["install"] }))
    await screen.findByRole("heading", { name: "Setup needs a fix" })
  })

  it("folds older completed steps and shows only one next step", async () => {
    const plan = [
      "runtime",
      "dependencies",
      "skills",
      "account",
      "credentials",
      "connection",
      "network",
      "sources",
      ...SOURCE_PLAN,
      "deep_context",
      "enrich",
      "index",
      "validate",
      "ready",
    ]
    const status = {
      ...INSTALL,
      plan,
      step: "enrich",
      status: "running",
      steps: Object.fromEntries(
        plan.slice(0, plan.indexOf("enrich")).map((step) => [step, { status: "completed", message: "Done" }]),
      ),
    }
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(new Response(JSON.stringify(status)))),
    )
    const { container } = mount()
    // Eight rows, six done: the oldest folds, five done stay, then the current row and one next.
    const history = await screen.findByRole("button", { name: "1 tasks completed" })
    expect(container.querySelectorAll('li[data-folded="false"]').length).toBe(7)
    expect(
      screen
        .getAllByText("Next")
        .filter((node) => node.closest("li")?.getAttribute("aria-hidden") === "false").length,
    ).toBe(1)
    fireEvent.click(history)
    expect(container.querySelectorAll('li[data-folded="false"]').length).toBe(8)
    expect(history.getAttribute("aria-expanded")).toBe("true")
  })

  it("completes the index row after validation", async () => {
    let status: InstallStatus = {
      ...INSTALL,
      plan: ["deep_context", "enrich", "index", "validate", "ready"],
      step: "enrich",
    }
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) =>
        Promise.resolve(
          new Response(
            JSON.stringify(url === "/api/status" ? { stage: "enrich", step: "", pending: {} } : status),
          ),
        ),
      ),
    )
    const { client } = mount()
    await screen.findByText("Enriching people")
    status = {
      ...status,
      step: "validate",
      status: "running",
      steps: {
        deep_context: { status: "completed", message: "Done" },
        enrich: { status: "completed", message: "Done" },
        index: { status: "completed", message: "Done" },
      },
    }
    await act(() => client.invalidateQueries({ queryKey: ["install"] }))
    await waitFor(() =>
      expect(screen.getByRole("list").querySelector('[aria-current="step"]')?.textContent).toContain(
        "Making your connections searchableWorking",
      ),
    )
    status = {
      ...status,
      step: "ready",
      status: "completed",
      steps: {
        ...status.steps,
        validate: { status: "completed", message: "Done" },
        ready: { status: "completed", message: "Done" },
      },
    }
    await act(() => client.invalidateQueries({ queryKey: ["install"] }))
    await waitFor(() =>
      expect(screen.getByRole("list").querySelector('[aria-current="step"]')?.textContent).toContain(
        "Making your connections searchableDone",
      ),
    )
  })

  it("shows an embedded QR and opens permission settings through the local server", async () => {
    let status: InstallStatus = {
      ...INSTALL,
      step: "whatsapp_login",
      status: "waiting",
      action: { kind: "qr", qr_url: "/api/install/qr?t=1" },
    }
    const fetch = vi.fn(() => Promise.resolve(new Response(JSON.stringify(status))))
    vi.stubGlobal("fetch", fetch)
    const { client } = mount()
    expect(
      (await screen.findByRole("img", { name: "Scan this QR code to link WhatsApp" })).getAttribute("src"),
    ).toBe("/api/install/qr?t=1")
    status = {
      ...status,
      step: "imessage_access",
      note: "Powerpacks reads your iMessage history to find the people you talk to. Drag Example into Full Disk Access and turn it on; I’ll continue automatically.",
      action: { kind: "permission" },
    }
    await act(() => client.invalidateQueries({ queryKey: ["install"] }))
    fireEvent.click(await screen.findByRole("button", { name: "Open settings & show the app" }))
    expect(fetch).toHaveBeenCalledWith(
      "/api/install/permissions",
      expect.objectContaining({ method: "POST" }),
    )
    expect(screen.getByText(/Drag Example into Full Disk Access/)).toBeTruthy()
  })

  it("counts the tasks done over the list", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(new Response(JSON.stringify(INSTALL)))),
    )
    mount()
    expect(await screen.findByText(/^\d+ of \d+ tasks completed$/)).toBeTruthy()
  })

  it("keeps the page mounted on unchanged reads, through waiting and failed", async () => {
    let status = INSTALL
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(new Response(JSON.stringify(status)))),
    )
    const renders = vi.fn()
    const { client, container } = mount(renders)
    await screen.findByText(INSTALL.message)
    const knot = container.querySelector("canvas")
    expect(knot).not.toBeNull()
    const before = renders.mock.calls.length
    await act(() => client.invalidateQueries({ queryKey: ["install"] }))
    expect(renders.mock.calls.length).toBe(before)
    expect(container.querySelector("canvas")).toBe(knot)

    status = { ...INSTALL, status: "waiting", message: "Answer in chat whether to add Gmail tools" }
    await act(() => client.invalidateQueries({ queryKey: ["install"] }))
    await screen.findByRole("heading", { name: "One thing to finish" })

    status = { ...INSTALL, status: "failed", message: "Installation was interrupted" }
    await act(() => client.invalidateQueries({ queryKey: ["install"] }))
    await screen.findByText(status.message)
    expect(screen.getByText("Your progress is saved. I can check this step and retry.")).toBeTruthy()
  })

  it("reconnects in place and restores saved completion after a server outage", async () => {
    let reachable = true
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        reachable
          ? Promise.resolve(
              new Response(
                JSON.stringify({ ...INSTALL, step: "ready", status: "completed", message: "Installed" }),
              ),
            )
          : Promise.reject(new TypeError("Failed to fetch")),
      ),
    )
    const { client } = mount()
    await screen.findByRole("heading", { name: "Powerpacks is installed" })
    reachable = false
    await act(() => client.invalidateQueries({ queryKey: ["install"] }))
    await screen.findByRole("heading", { name: "Reconnecting to Powerpacks" })
    reachable = true
    await act(() => client.invalidateQueries({ queryKey: ["install"] }))
    await waitFor(() => expect(screen.getByRole("heading", { name: "Powerpacks is installed" })).toBeTruthy())
  })

  it("shows account waits, skipped login, and verified network without claiming early readiness", async () => {
    let status: InstallStatus = {
      ...INSTALL,
      step: "account",
      status: "waiting",
      message: "Please sign in to Powerset",
      steps: { account: { status: "waiting", message: "Waiting for account login" } },
    }
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(new Response(JSON.stringify(status)))),
    )
    const { client } = mount()
    await screen.findByRole("heading", { name: "Waiting for you to sign in" })
    expect(screen.getByText("Please sign in to Powerset")).toBeTruthy()

    status = {
      ...status,
      step: "network",
      status: "waiting",
      message: "Your personal network is empty. Would you like to sign in with another account?",
      account_email: "casey@example.com",
      network_name: "Personal Network",
      person_count: 0,
      steps: {
        account: { status: "skipped", message: "Already signed in" },
        network: { status: "waiting", message: "Choose another account or connect contacts" },
      },
    }
    await act(() => client.invalidateQueries({ queryKey: ["install"] }))
    await screen.findByText("Signing you in")
    // The account and network show in the sidebar footer, not here.
    expect(screen.queryByText("casey@example.com")).toBeNull()
    expect(screen.queryByRole("heading", { name: "Powerpacks is ready" })).toBeNull()

    status = {
      ...status,
      step: "ready",
      status: "completed",
      person_count: 4,
      message: "Ready. Your network is small; another account may have more people.",
    }
    await act(() => client.invalidateQueries({ queryKey: ["install"] }))
    await screen.findByRole("heading", { name: "Powerpacks is ready" })
    expect(screen.queryByText("Personal Network · 4 people")).toBeNull()
    expect(screen.getByText(status.message)).toBeTruthy()
  })

  it("counts how long a running step has taken, and only while it runs", async () => {
    const started = new Date(Date.now() - 125_000).toISOString().replace(/\.\d{3}Z$/, "Z")
    let status: InstallStatus = {
      ...INSTALL,
      step: "deep_context",
      message: "Learning about your contacts: 40 of 298",
      step_started_at: started,
      steps: { deep_context: { status: "running", message: "Learning about your contacts: 40 of 298" } },
    }
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(new Response(JSON.stringify(status)))),
    )
    const { client, container } = mount()
    await screen.findByText("Learning about your contacts: 40 of 298")
    expect(container.querySelector(".install-elapsed")?.textContent).toMatch(/^2m 0[5-9]s so far$/)
    status = { ...status, status: "waiting", action: { kind: "owner" } }
    await act(() => client.invalidateQueries({ queryKey: ["install"] }))
    await waitFor(() => expect(container.querySelector(".install-elapsed")?.textContent).toBe(""))
  })

  it("keeps owner lookup busy and returns failures to the editable form", async () => {
    let rejectLookup!: (error: Error) => void
    const invoke = vi.fn(
      () =>
        new Promise<void>((_, reject) => {
          rejectLookup = reject
        }),
    )
    vi.stubGlobal("__TAURI__", {
      core: { invoke },
      event: { listen: () => Promise.resolve(() => undefined) },
    })
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve(
          new Response(
            JSON.stringify({
              ...INSTALL,
              step: "deep_context",
              status: "waiting",
              action: { kind: "owner" },
            }),
          ),
        ),
      ),
    )
    mount()
    const input = await screen.findByLabelText("Your LinkedIn profile")
    fireEvent.change(input, { target: { value: "https://linkedin.com/in/jordan-example" } })
    const button = screen.getByRole("button", { name: "Continue" })
    fireEvent.click(button)
    await waitFor(() => expect(button).toHaveProperty("disabled", true))
    expect(input).toHaveProperty("disabled", true)
    await act(async () => {
      rejectLookup(new Error("Could not load that LinkedIn profile."))
      await Promise.resolve()
    })
    expect(await screen.findByRole("alert")).toHaveProperty(
      "textContent",
      "Could not load that LinkedIn profile.",
    )
    expect(input).toHaveProperty("disabled", false)
    expect(input).toHaveProperty("value", "https://linkedin.com/in/jordan-example")
    expect(invoke).toHaveBeenCalledTimes(1)
  })

  it("in the desktop app, a failed step offers Try again", async () => {
    const invoke = vi.fn(() => Promise.resolve(null))
    vi.stubGlobal("__TAURI__", {
      core: { invoke },
      event: { listen: () => Promise.resolve(() => undefined) },
    })
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve(
          new Response(
            JSON.stringify({
              ...INSTALL,
              step: "gmail_login",
              status: "failed",
              event: "gmail.connect.failed",
              installer_pid: 0,
              action: { kind: "error" },
            }),
          ),
        ),
      ),
    )
    mount()
    fireEvent.click(await screen.findByRole("button", { name: "Try again" }))
    await waitFor(() => expect(invoke).toHaveBeenCalledWith("onboard_continue", { answer: {} }))
  })
})
