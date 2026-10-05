import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { Profiler } from "react"
import { afterEach, describe, expect, it, vi } from "vitest"

import type { InstallStatus } from "@/types/install"

import { InstallPage } from "./InstallPage"
import PROSE from "./prose.fixture.json"

const INSTALL: InstallStatus = {
  primitive: "powerpacks_install",
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
  it("shows a stopped WhatsApp connection as paused rather than broken", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve(
          new Response(
            JSON.stringify({
              ...INSTALL,
              plan: ["whatsapp_tools", "whatsapp_login", "whatsapp_sync", "deep_context"],
              step: "whatsapp_login",
              status: "waiting",
              message: "Setup paused. I can resume it from here.",
              action: { kind: "resume", command: "bin/onboard" },
              steps: { whatsapp_tools: { status: "completed" }, whatsapp_login: { status: "waiting" } },
            }),
          ),
        ),
      ),
    )
    mount()
    await screen.findByRole("heading", { name: "Setup paused" })
    expect(screen.getByText("Logging in to your accounts").closest("li")?.textContent).toContain("Paused")
    expect(screen.queryByText("Needs a fix")).toBeNull()
  })

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
    await screen.findByText("Logging in to your accounts")
    const current = () => screen.getByRole("list").querySelector('[aria-current="step"]')?.textContent
    expect(current()).toContain("Logging in to your accountsWaiting")
    expect(screen.getByRole("list").textContent).toContain("Syncing LinkedIn")

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
    await waitFor(() => expect(current()).toContain("Syncing LinkedInWorking"))
    expect(screen.getByText("Logging in to your accounts").closest("li")?.textContent).toContain("Done")

    for (const step of SOURCE_PLAN) {
      status = { ...status, step, status: "failed", steps: {} }
      await act(() => client.invalidateQueries({ queryKey: ["install"] }))
      await waitFor(() => {
        expect(current()).toContain("Needs a fix")
        expect(current()).toContain(LOGINS.includes(step) ? "Logging in" : "Syncing")
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
    await screen.findByText("Installing Powerpacks")
    expect(screen.queryByText("Building your search index")).toBeNull()
  })

  it("reads shared processing progress, then switches to index progress and stops on failure", async () => {
    let status: InstallStatus = { ...INSTALL, step: "enrich", message: "Enriching contacts" }
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) =>
        Promise.resolve(
          new Response(
            JSON.stringify(
              url === "/api/status"
                ? {
                    stage: "enrich",
                    step: "research",
                    pending: { lookups: 3 },
                  }
                : status,
            ),
          ),
        ),
      ),
    )
    const { client, container } = mount()
    await screen.findByText("Looking up 3 people")
    status = {
      ...INSTALL,
      step: "index",
      index_progress: {
        status: "running",
        progress: 0.4,
        message: "Building search records",
        payload: { contacts: 3 },
      },
    }
    await act(() => client.invalidateQueries({ queryKey: ["install"] }))
    await screen.findByText("Building search records")
    expect(screen.getByRole("progressbar").getAttribute("value")).toBe("0.4")
    status = {
      ...status,
      index_progress: { status: "failed", message: "Index download failed", payload: {} },
    }
    await act(() => client.invalidateQueries({ queryKey: ["install"] }))
    await screen.findByRole("heading", { name: "Setup needs a fix" })
    expect(container.querySelector(".enrich-orbit")).toBeNull()
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
      "review",
      "index",
      "validate",
      "ready",
    ]
    const status = {
      ...INSTALL,
      plan,
      step: "review",
      status: "waiting",
      steps: Object.fromEntries(
        plan.slice(0, plan.indexOf("review")).map((step) => [step, { status: "completed", message: "Done" }]),
      ),
    }
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(new Response(JSON.stringify(status)))),
    )
    const { container } = mount()
    const history = await screen.findByRole("button", { name: "3 tasks completed" })
    expect(container.querySelectorAll('li[data-folded="false"]').length).toBe(7)
    expect(
      screen
        .getAllByText("Next")
        .filter((node) => node.closest("li")?.getAttribute("aria-hidden") === "false").length,
    ).toBe(1)
    fireEvent.click(history)
    expect(container.querySelectorAll('li[data-folded="false"]').length).toBe(10)
    expect(history.getAttribute("aria-expanded")).toBe("true")
  })

  it("shows review only when needed and completes the index row after validation", async () => {
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
    await screen.findByText("Enriching your contacts")
    expect(screen.queryByText("Waiting for your review")).toBeNull()
    status = {
      ...status,
      plan: ["deep_context", "enrich", "review", "index", "validate", "ready"],
      step: "review",
      status: "waiting",
      action: { kind: "review" },
      steps: {
        deep_context: { status: "completed", message: "Done" },
        enrich: { status: "completed", message: "Done" },
      },
    }
    await act(() => client.invalidateQueries({ queryKey: ["install"] }))
    await screen.findByRole("button", { name: "Review contacts" })
    expect(screen.getByRole("list").querySelector('[aria-current="step"]')?.textContent).toContain(
      "Waiting for your reviewWaiting",
    )
    status = {
      ...status,
      step: "validate",
      status: "running",
      action: null,
      steps: {
        ...status.steps,
        review: { status: "completed", message: "Done" },
        index: { status: "completed", message: "Done" },
      },
    }
    await act(() => client.invalidateQueries({ queryKey: ["install"] }))
    await screen.findByText("Review completed")
    expect(screen.getByRole("list").querySelector('[aria-current="step"]')?.textContent).toContain(
      "Building your search indexWorking",
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
        "Building your search indexDone",
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

  it("keeps the page mounted on unchanged reads and stops the progress orbit while waiting or failed", async () => {
    let status = INSTALL
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(new Response(JSON.stringify(status)))),
    )
    const renders = vi.fn()
    const { client, container } = mount(renders)
    await screen.findByText(INSTALL.message)
    const shape = container.querySelector(".enrich-shape")
    const before = renders.mock.calls.length
    await act(() => client.invalidateQueries({ queryKey: ["install"] }))
    expect(renders.mock.calls.length).toBe(before)
    expect(container.querySelector(".enrich-shape")).toBe(shape)

    status = { ...INSTALL, status: "waiting", message: "Answer in chat whether to add Gmail tools" }
    await act(() => client.invalidateQueries({ queryKey: ["install"] }))
    await screen.findByRole("heading", { name: "One thing to finish" })
    expect(container.querySelector(".enrich-orbit")).toBeNull()

    status = { ...INSTALL, status: "failed", message: "Installation was interrupted" }
    await act(() => client.invalidateQueries({ queryKey: ["install"] }))
    await screen.findByText(status.message)
    expect(container.querySelector(".enrich-orbit")).toBeNull()
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
    const { client, container } = mount()
    await screen.findByRole("heading", { name: "Powerpacks is installed" })
    expect(container.querySelector(".enrich-orbit")).toBeNull()
    reachable = false
    await act(() => client.invalidateQueries({ queryKey: ["install"] }))
    await screen.findByRole("heading", { name: "Reconnecting to Powerpacks" })
    reachable = true
    await act(() => client.invalidateQueries({ queryKey: ["install"] }))
    await waitFor(() => expect(screen.getByRole("heading", { name: "Powerpacks is installed" })).toBeTruthy())
    expect(container.querySelector(".enrich-orbit")).toBeNull()
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
    const { client, container } = mount()
    await screen.findByRole("heading", { name: "Waiting for you to sign in" })
    expect(container.querySelector(".enrich-orbit")).toBeNull()
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
    await screen.findByText("casey@example.com")
    expect(screen.getByText("Personal Network · 0 people")).toBeTruthy()
    expect(screen.getByText("Installing Powerpacks")).toBeTruthy()
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
    expect(screen.getByText("Personal Network · 4 people")).toBeTruthy()
    expect(screen.getByText(status.message)).toBeTruthy()
    expect(container.querySelector(".enrich-orbit")).toBeNull()
  })
})
