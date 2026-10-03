import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { act, cleanup, render, screen, waitFor } from "@testing-library/react"
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
    expect(
      screen.getByText(
        "Your agent can read the saved error and retry this step. Check chat for the next action.",
      ),
    ).toBeTruthy()
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
    expect(screen.getByText("Already signed in")).toBeTruthy()
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
