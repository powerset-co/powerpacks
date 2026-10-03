import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { Profiler } from "react"
import { afterEach, describe, expect, it, vi } from "vitest"

import type { InstallStatus } from "@/types/install"

import { InstallPage } from "./InstallPage"

const INSTALL: InstallStatus = {
  primitive: "powerpacks_install",
  status: "running",
  step: "dependencies",
  message: "Installing what Powerpacks needs",
  log_path: "/synthetic/.powerpacks/install/install.log",
  retry_command: "bin/bootstrap --no-tools",
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
  it("combines source preparation and imports into link and sync steps", async () => {
    const plan = [
      "imessage_access",
      "imessage_import",
      "whatsapp_tools",
      "whatsapp_login",
      "whatsapp_sync",
      "whatsapp_import",
      "gmail_tools",
      "gmail_login",
      "gmail_sync",
      "gmail_import",
    ]
    let status: InstallStatus = {
      ...INSTALL,
      plan,
      step: "gmail_login",
      status: "waiting",
      steps: Object.fromEntries(
        plan.slice(0, 7).map((step) => [step, { status: "completed", message: "Done" }]),
      ),
    }
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(new Response(JSON.stringify(status)))),
    )
    const { client } = mount()
    await screen.findByText("Link Gmail")
    expect(screen.getByRole("list").textContent.replace(/[✓•○]/g, "")).toBe(
      "Link iMessageDoneSync iMessageDoneLink WhatsAppDoneSync WhatsAppDoneLink GmailWaitingSync GmailNext",
    )
    status = { ...status, step: "gmail_tools", status: "completed" }
    await act(() => client.invalidateQueries({ queryKey: ["install"] }))
    await waitFor(() =>
      expect(screen.getByRole("list").querySelector('[aria-current="step"]')?.textContent).toContain(
        "Link GmailWorking",
      ),
    )
    for (const step of plan) {
      status = { ...status, step, status: "failed", steps: {} }
      await act(() => client.invalidateQueries({ queryKey: ["install"] }))
      await waitFor(() => {
        const row = screen.getByRole("list").querySelector('[aria-current="step"]')
        expect(row?.textContent).toContain("Needs a fix")
        expect(row?.textContent).toContain(
          step.includes("tools") || step.includes("login") || step.includes("access") ? "Link" : "Sync",
        )
      })
    }
  })

  it("reads shared processing progress, then switches to index progress and stops on failure", async () => {
    let status: InstallStatus = { ...INSTALL, step: "deep_context", message: "Preparing contacts" }
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
      "imessage_access",
      "imessage_import",
    ]
    const status = {
      ...INSTALL,
      plan,
      step: "sources",
      steps: Object.fromEntries(
        plan.slice(0, 7).map((step) => [step, { status: "completed", message: "Done" }]),
      ),
    }
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(new Response(JSON.stringify(status)))),
    )
    const { container } = mount()
    const history = await screen.findByRole("button", { name: "2 earlier steps done" })
    expect(container.querySelectorAll('li[data-folded="false"]').length).toBe(7)
    expect(
      screen
        .getAllByText("Next")
        .filter((node) => node.closest("li")?.getAttribute("aria-hidden") === "false").length,
    ).toBe(1)
    fireEvent.click(history)
    expect(container.querySelectorAll('li[data-folded="false"]').length).toBe(9)
    expect(history.getAttribute("aria-expanded")).toBe("true")
  })

  it("submits chosen sources without starting unselected imports", async () => {
    const status = { ...INSTALL, step: "sources", status: "waiting", action: { kind: "sources" } }
    const fetch = vi.fn((_url: string, options?: RequestInit) =>
      Promise.resolve(
        new Response(JSON.stringify(options?.method === "POST" ? { status: "started" } : status)),
      ),
    )
    vi.stubGlobal("fetch", fetch)
    mount()
    fireEvent.click(await screen.findByRole("checkbox", { name: "iMessage" }))
    fireEvent.click(screen.getByRole("button", { name: "Continue" }))
    await waitFor(() =>
      expect(fetch.mock.calls.some((call) => call[0] === "/api/install/sources")).toBe(true),
    )
    const post = fetch.mock.calls.find((call) => call[0] === "/api/install/sources")
    const body = post?.[1]?.body
    expect(typeof body).toBe("string")
    expect(JSON.parse(typeof body === "string" ? body : "{}")).toMatchObject({ sources: ["imessage"] })
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
      action: { kind: "permission", app_path: "/Applications/Example.app" },
    }
    await act(() => client.invalidateQueries({ queryKey: ["install"] }))
    fireEvent.click(await screen.findByRole("button", { name: "Open settings & show the app" }))
    expect(fetch).toHaveBeenCalledWith(
      "/api/install/permissions",
      expect.objectContaining({ method: "POST" }),
    )
    expect(screen.getByText("Example")).toBeTruthy()
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
    expect(screen.getByText("Skipped")).toBeTruthy()
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
