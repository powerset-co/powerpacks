import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"

import type { UploadStatus } from "@/lib/api/upload"
import { PLAN, uploadResponse, uploadStatus } from "@/testing/upload-fixture"

import { ShareUpload } from "./ShareUpload"

const POLLED = { timeout: 2500 }
const FINISHED = "2026-09-27T12:00:00Z"
const SHARED = { finished_at: FINISHED, status: "completed", uploaded: 3, skipped: 125 } as const

type Answer = UploadStatus | Response | Error
interface Routes {
  get: () => Answer
  check?: () => Answer
  upload?: () => Answer
}

function setsResponse(): Response {
  return new Response(JSON.stringify({ sets: [], invites: [], shared: 0, default_set_id: "" }), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  })
}

/** fetch over the three upload routes; each call asks its route for the answer now. */
function serve(routes: Routes) {
  const fetch = vi.fn((url: string, init?: RequestInit) => {
    // The share menu beside the button reads the sets once; none here.
    if (url.includes("/sets")) return Promise.resolve(setsResponse())
    const route = init?.method !== "POST" ? routes.get : url.endsWith("/check") ? routes.check : routes.upload
    const answer = route?.() ?? new Error(`unexpected ${url}`)
    if (answer instanceof Error) return Promise.reject(answer)
    return Promise.resolve(answer instanceof Response ? answer : uploadResponse(answer))
  })
  vi.stubGlobal("fetch", fetch)
  return fetch
}

function posts(fetch: ReturnType<typeof serve>): string[] {
  return fetch.mock.calls.filter(([, init]) => init?.method === "POST").map(([url]) => url)
}

let client: QueryClient

function show(onToast = vi.fn()) {
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <ShareUpload onToast={onToast} />
    </QueryClientProvider>,
  )
  return onToast
}

/** Opens the share dialog. */
async function openDialog(label: string) {
  await waitFor(() => expect(client.getQueryData(["people-upload"])).toBeTruthy())
  fireEvent.click(await screen.findByRole("button", { name: label }))
  return screen.findByRole("dialog")
}

function buttons(dialog: HTMLElement): string[] {
  return within(dialog)
    .getAllByRole("button")
    .map((button) => button.textContent)
    .filter((text) => text !== "×")
}

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

describe("ShareUpload trigger", () => {
  it.each([
    [uploadStatus(), "Share network", "Never shared"],
    [uploadStatus({ status: "completed", last_upload: SHARED }), "Share changes", "Shared 128 · Sep 27"],
    [
      uploadStatus({ status: "interrupted", last_upload: { ...SHARED, status: "interrupted" } }),
      "Share changes",
      "Upload interrupted · Sep 27",
    ],
    [uploadStatus({ status: "uploading", last_upload: SHARED }), "View upload", "Shared 128 · Sep 27"],
    [uploadStatus({ status: "checking" }), "View upload", "Never shared"],
  ])("shows the button without the last-upload caption (%#)", async (status, label, line) => {
    serve({ get: () => status })
    show()
    expect(await screen.findByRole("button", { name: label })).toBeTruthy()
    expect(screen.queryByText(line)).toBeNull()
  })
})

describe("ShareUpload dialog", () => {
  it("opens a checked network on the saved status without posting", async () => {
    const fetch = serve({ get: () => uploadStatus({ status: "ready", plan: PLAN }) })
    show()
    const dialog = await openDialog("Share network")
    expect(within(dialog).getByRole("heading", { name: "Ready to share" })).toBeTruthy()
    expect(buttons(dialog)).toEqual(["Close", "Check again", "Confirm sharing"])
    expect(posts(fetch)).toEqual([])
  })

  it("checks again when Share changes opens after a finished upload", async () => {
    let state = uploadStatus({ status: "completed", plan: PLAN, last_upload: SHARED })
    const fetch = serve({
      get: () => state,
      check: () => (state = uploadStatus({ status: "checking", plan: PLAN, last_upload: SHARED })),
    })
    show()
    const dialog = await openDialog("Share changes")
    await within(dialog).findByRole("heading", { name: "Checking your network" })
    expect(posts(fetch)).toEqual(["/api/people/upload/check"])
  })

  it("checks a never-checked network on the first open, then lists the plan", async () => {
    let state = uploadStatus()
    const fetch = serve({
      get: () => state,
      check: () => (state = uploadStatus({ status: "checking", message: "Checking who can upload." })),
    })
    show()
    const dialog = await openDialog("Share network")
    await within(dialog).findByText("Checking who can upload.")
    expect(within(dialog).getByRole("heading", { name: "Checking your network" })).toBeTruthy()
    expect(within(dialog).getByRole("progressbar").hasAttribute("aria-valuenow")).toBe(false)
    expect(buttons(dialog)).toEqual(["Close"])
    state = uploadStatus({ status: "ready", plan: PLAN })
    await within(dialog).findByRole("heading", { name: "Ready to share" }, POLLED)
    const terms = [...dialog.querySelectorAll("dt")].map((term) => term.textContent)
    expect(terms).toEqual([
      "Marked Share",
      "With LinkedIn, will upload",
      "Without LinkedIn, stay local",
      "New to the cloud",
      "Already shared",
    ])
    expect(posts(fetch)).toEqual(["/api/people/upload/check"])
  })

  it("uploads only on confirm, sending the check it displayed, counts people, then shows the result", async () => {
    let state = uploadStatus({ status: "ready", plan: PLAN, checked: "digest-1" })
    const fetch = serve({
      get: () => state,
      upload: () =>
        (state = uploadStatus({
          status: "uploading",
          plan: PLAN,
          message: "Uploading people.",
          progress: { total: 128, uploaded: 2, skipped: 60, namespaces: {} },
        })),
    })
    show()
    const dialog = await openDialog("Share network")
    fireEvent.click(within(dialog).getByRole("button", { name: "Confirm sharing" }))
    await within(dialog).findByRole("heading", { name: "Uploading your network" })
    expect(within(dialog).getByText("Closing keeps the upload running.")).toBeTruthy()
    expect(within(dialog).getByRole("progressbar").getAttribute("aria-valuenow")).toBe("62")
    expect(within(dialog).getByText("62 of 128 people")).toBeTruthy()
    expect(buttons(dialog)).toEqual(["Close"])
    state = uploadStatus({ status: "completed", plan: PLAN, last_upload: SHARED })
    await within(dialog).findByRole("heading", { name: "Your network is shared" }, POLLED)
    expect(within(dialog).getByText("Uploaded").nextSibling?.textContent).toBe("3")
    expect(within(dialog).getByText("Already up to date").nextSibling?.textContent).toBe("125")
    expect(within(dialog).getByText(/^Finished Sep 27/)).toBeTruthy()
    expect(posts(fetch)).toEqual(["/api/people/upload"])
    const confirm = fetch.mock.calls.find(
      ([url, init]) => url === "/api/people/upload" && init?.method === "POST",
    )
    const sent = confirm?.[1]?.body
    expect(typeof sent === "string" ? JSON.parse(sent) : sent).toEqual({ checked: "digest-1" })
  })

  it("says up to date when the run had nothing to write", async () => {
    let state = uploadStatus({ status: "uploading", plan: PLAN, last_upload: SHARED })
    serve({ get: () => state })
    show()
    const dialog = await openDialog("View upload")
    state = uploadStatus({ status: "completed", plan: PLAN, last_upload: { ...SHARED, uploaded: 0 } })
    await within(dialog).findByRole("heading", { name: "Your network is up to date" }, POLLED)
    // Nothing follows a finished upload: Close alone, and it takes the colour.
    expect(buttons(dialog)).toEqual(["Close"])
    expect(within(dialog).getByText("Close", { selector: "button" }).className).toContain("primary")
  })

  it.each([
    ["failed", "check", "Check failed"],
    ["failed", "upload", "Upload failed"],
    ["interrupted", "upload", "Upload interrupted"],
  ] as const)("shows a %s %s as its title and the server's sentence", async (state, action, title) => {
    serve({ get: () => uploadStatus({ status: state, failed_action: action, error: "The cloud said no." }) })
    show()
    const dialog = await openDialog("Share network")
    expect(within(dialog).getByRole("heading", { name: title })).toBeTruthy()
    expect(within(dialog).getByText("The cloud said no.").className).toContain("text-bad")
    expect(within(dialog).queryByRole("progressbar")).toBeNull()
    expect(buttons(dialog)).toEqual(["Close", "Check again"])
  })

  it("resumes a failed upload once the new check is ready", async () => {
    const last = { ...SHARED, status: "interrupted" } as const
    let state = uploadStatus({ status: "interrupted", failed_action: "upload", last_upload: last })
    serve({
      get: () => state,
      check: () => (state = uploadStatus({ status: "ready", plan: PLAN, last_upload: last })),
    })
    show()
    const dialog = await openDialog("Share changes")
    fireEvent.click(within(dialog).getByRole("button", { name: "Check again" }))
    await within(dialog).findByRole("heading", { name: "Ready to share" })
    expect(buttons(dialog)).toEqual(["Close", "Check again", "Resume sharing"])
  })

  it("shows a refused confirm's sentence and offers only a new check", async () => {
    const changed = "Your network changed since the check. Check again."
    const ready = () => uploadStatus({ status: "ready", plan: PLAN })
    serve({
      get: ready,
      check: ready,
      upload: () => new Response(JSON.stringify({ error: changed }), { status: 409 }),
    })
    show()
    const dialog = await openDialog("Share network")
    fireEvent.click(within(dialog).getByRole("button", { name: "Confirm sharing" }))
    expect((await within(dialog).findByText(changed)).className).toContain("text-bad")
    expect(buttons(dialog)).toEqual(["Close", "Check again"])
    fireEvent.click(within(dialog).getByRole("button", { name: "Check again" }))
    await within(dialog).findByRole("button", { name: "Confirm sharing" })
    expect(within(dialog).queryByText(changed)).toBeNull()
  })

  it("keeps polling after a failed start and follows the run it started", async () => {
    let state = uploadStatus({ status: "ready", plan: PLAN })
    serve({
      get: () => state,
      check: () => {
        state = uploadStatus({ status: "checking", message: "Checking who can upload." })
        return new TypeError("Failed to fetch")
      },
    })
    show()
    const dialog = await openDialog("Share network")
    fireEvent.click(within(dialog).getByRole("button", { name: "Check again" }))
    await within(dialog).findByText("Checking who can upload.")
    state = uploadStatus({ status: "ready", plan: PLAN })
    await within(dialog).findByRole("heading", { name: "Ready to share" }, POLLED)
  })

  it("says it is reconnecting when a poll fails, never that the upload failed", async () => {
    let answer: Answer = uploadStatus({ status: "uploading", plan: PLAN, message: "Uploading people." })
    serve({ get: () => answer })
    show()
    const dialog = await openDialog("View upload")
    answer = new TypeError("Failed to fetch")
    await within(dialog).findByText("Reconnecting…", undefined, POLLED)
    expect(within(dialog).getByRole("heading", { name: "Uploading your network" })).toBeTruthy()
    answer = uploadStatus({ status: "uploading", plan: PLAN, message: "Uploading people." })
    await within(dialog).findByText("Uploading people.", undefined, POLLED)
  })

  it("toasts a run that finishes while the dialog is closed", async () => {
    let state = uploadStatus({ status: "uploading", plan: PLAN })
    serve({ get: () => state })
    const onToast = show()
    await screen.findByRole("button", { name: "View upload" })
    state = uploadStatus({
      status: "completed",
      plan: PLAN,
      last_upload: SHARED,
      progress: { total: 128, uploaded: 3, skipped: 125, namespaces: {} },
    })
    await waitFor(() => expect(onToast).toHaveBeenCalledWith("Shared 128 people."), POLLED)
    expect(screen.getByRole("button", { name: "Share changes" })).toBeTruthy()
  })

  it("polls only while a check or upload runs", async () => {
    const fetch = serve({ get: () => uploadStatus({ status: "ready", plan: PLAN }) })
    show()
    await screen.findByRole("button", { name: "Share network" })
    await new Promise((resolve) => setTimeout(resolve, 1300))
    expect(fetch.mock.calls.filter(([url]) => !url.includes("/sets"))).toHaveLength(1)
  })
})
