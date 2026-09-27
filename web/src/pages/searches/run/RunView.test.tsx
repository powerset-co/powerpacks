import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import type { FeedbackOutcome } from "@/lib/searches/feedback"
import { RANKED_NAMES, RUN, RUN_ID } from "@/testing/searches-fixture"
import type { FeedbackRecord, Tagged } from "@/types/searches"

import type { Feedback } from "../hooks/useFeedback"
import { searchRunKey, useSearchRun } from "../hooks/useSearchRun"
import { RunView } from "./RunView"

// jsdom has no layout, Web Animations, matchMedia or ResizeObserver; the virtualizer reads
// offset sizes. Reduced motion: presence mounts and unmounts at once.
beforeEach(() => {
  Object.defineProperty(HTMLElement.prototype, "offsetHeight", { configurable: true, get: () => 800 })
  Object.defineProperty(HTMLElement.prototype, "offsetWidth", { configurable: true, get: () => 1200 })
  HTMLElement.prototype.animate = vi.fn()
  HTMLElement.prototype.scrollTo = vi.fn()
  vi.stubGlobal("matchMedia", (media: string) => ({
    matches: true,
    media,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
  }))
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {
        // Layout is stubbed; nothing to observe.
      }
      unobserve() {
        // See observe.
      }
      disconnect() {
        // See observe.
      }
    },
  )
})
afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  sessionStorage.clear()
})

/** GET /searches/tags answers `initial`; each POST /searches/tags is recorded and accepted. */
function tagServer(initial: Tagged | null) {
  const saves: unknown[] = []
  vi.stubGlobal(
    "fetch",
    vi.fn((_url: string, init?: RequestInit) => {
      if (init?.body instanceof URLSearchParams) {
        const tagged: unknown = JSON.parse(init.body.get("tagged") ?? "null")
        saves.push(tagged)
        return Promise.resolve(new Response(JSON.stringify({ ok: true })))
      }
      return Promise.resolve(new Response(JSON.stringify({ tagged: initial })))
    }),
  )
  return saves
}

const NOT_STOPPED: Pick<Feedback, "pending" | "failure"> = { pending: [], failure: null }

// The run as RunPane reads it: from the query cache, so a saved score shows through it.
function Harness({ feedback, onToast }: { feedback: Feedback; onToast: () => void }) {
  const run = useSearchRun(RUN_ID)
  return run.data ? (
    <RunView payload={run.data} status="completed" feedback={feedback} onToast={onToast} />
  ) : null
}

function renderRun(tags: Tagged | null = null, stopped: Pick<Feedback, "pending" | "failure"> = NOT_STOPPED) {
  const saves = tagServer(tags)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  client.setQueryData(searchRunKey(RUN_ID), RUN)
  const submit = vi.fn((_record: FeedbackRecord) => Promise.resolve<FeedbackOutcome>("sent"))
  const retry = vi.fn(() => Promise.resolve())
  const feedback: Feedback = { ...stopped, submit, retry, signIn: () => Promise.resolve() }
  const onToast = vi.fn()
  render(
    <QueryClientProvider client={client}>
      <Harness feedback={feedback} onToast={onToast} />
    </QueryClientProvider>,
  )
  return { saves, submit, retry, onToast }
}

const tableHeadings = () =>
  [...document.querySelectorAll<HTMLElement>(".results-heading")].map((heading) => heading.textContent)

const rowNames = () =>
  [...document.querySelectorAll<HTMLElement>(".result-row .result-who b")].map((name) => name.textContent)

function row(personId: string) {
  return within(document.querySelector<HTMLElement>(`[data-person-id='${personId}']`) ?? document.body)
}

// The row's line: a click on it toggles the drawer; the actions inside keep their own clicks.
function main(personId: string): HTMLElement {
  const line = document.querySelector<HTMLElement>(`[data-person-id='${personId}'] .result-line`)
  if (!line) throw new Error(`no row for ${personId}`)
  return line
}

const key = (name: string) => act(() => void fireEvent.keyDown(document.body, { key: name }))
const drawer = () => document.querySelector<HTMLElement>("[data-drawer]")
const drawerName = () => drawer()?.querySelector("h2")?.textContent
const drawerOpen = () => drawer()?.getAttribute("data-open")
const bar = () => within(screen.getByRole("toolbar", { name: "Review" }))

async function tagsLoaded(name = "Jordan Bravo") {
  await waitFor(() =>
    expect(screen.getByRole("button", { name: `Add tag to ${name}` })).toHaveProperty("disabled", false),
  )
}

describe("RunView", () => {
  it("shows the header, the pond chain and both score tables in the documented order", () => {
    renderRun()
    const header = within(screen.getByRole("banner"))
    expect(header.getByRole("heading", { name: "Backend Engineer" })).toBeTruthy()
    expect(header.getByText("Search complete")).toBeTruthy()
    expect(header.getByText("6 people")).toBeTruthy()
    expect(header.getByText("$1.25")).toBeTruthy()
    expect(header.getByRole("button", { name: "Send feedback about Backend Engineer" })).toBeTruthy()
    expect([...document.querySelectorAll("[data-pond] .pond-count")].map((pond) => pond.textContent)).toEqual(
      ["Kept 3 of 40", "Kept 3 of 80"],
    )
    expect(tableHeadings()).toEqual(["Jev qualification scores", "Rating-based scores"])
    expect(rowNames()).toEqual(RANKED_NAMES.flat())
  })

  it("shows scores, missing scores as words, labels and sources", () => {
    renderRun()
    const jordan = row("p-jordan")
    expect(jordan.getByText("5/5")).toBeTruthy()
    expect(jordan.getByText("Suggested pin")).toBeTruthy()
    expect(jordan.getByText("Taste 4.5")).toBeTruthy()
    expect(jordan.getByText("Team #2")).toBeTruthy()
    expect(jordan.getAllByRole("img").map((icon) => icon.getAttribute("aria-label"))).toEqual([
      "Gmail",
      "LinkedIn",
    ])
    expect(jordan.getByText("DK")).toBeTruthy()
    expect(row("p-casey").getByText("Not judged")).toBeTruthy()
    fireEvent.click(screen.getByRole("button", { name: "Labels" }))
    expect(
      document.querySelector("[data-person-id='p-jordan'] .result-badges")?.getAttribute("data-labels"),
    ).toBe("false")
  })

  it("shows every source family, email and message counts, and where the person is", () => {
    renderRun()
    const jordan = row("p-jordan")
    expect(jordan.getByTitle("Gmail").textContent).toBe("42")
    expect(jordan.getByText("Staff Engineer · Example Labs · Oakland, CA")).toBeTruthy()
    expect(
      row("p-casey")
        .getAllByRole("img")
        .map((icon) => icon.getAttribute("aria-label")),
    ).toEqual(["X", "Contacts export"])
  })

  it("lists who a person came through, with their counts, in the drawer", () => {
    renderRun()
    fireEvent.click(main("p-jordan"))
    const operators = within(document.querySelector<HTMLElement>("[data-operators]") ?? document.body)
    expect(operators.getByText("Drew Kilo")).toBeTruthy()
    expect(operators.getByText("Gmail · LinkedIn · 42 emails")).toBeTruthy()
    expect(operators.getByText("LinkedIn")).toBeTruthy()
  })

  it("folds the job description open under the header", () => {
    renderRun()
    const toggle = screen.getByRole("button", { name: /Job description/ })
    expect(toggle.getAttribute("aria-expanded")).toBe("false")
    fireEvent.click(toggle)
    expect(toggle.getAttribute("aria-expanded")).toBe("true")
    expect(screen.getByText("Build the payments platform with a small backend team.")).toBeTruthy()
  })

  it("opens a row in the drawer, swaps to another row, and closes on the same row", () => {
    renderRun()
    fireEvent.click(main("p-jordan"))
    expect(drawerOpen()).toBe("true")
    expect(drawerName()).toBe("Jordan Bravo")
    const panel = within(drawer() ?? document.body)
    expect(panel.getByText("Built this exact system twice.")).toBeTruthy()
    expect(panel.getByText("Example University")).toBeTruthy()
    expect(panel.getByText(/^Rank 2 of 6 by likeness/)).toBeTruthy()
    expect(document.querySelector("[data-person-id='p-jordan']")?.getAttribute("data-open")).toBe("true")
    expect(document.querySelector(".result-chevron")).toBeNull()
    fireEvent.click(main("p-casey"))
    expect(drawerName()).toBe("Casey Delta")
    fireEvent.click(main("p-casey"))
    expect(drawerOpen()).toBe("false")
    expect(screen.queryByRole("toolbar", { name: "Review" })).toBeNull()
  })

  it("follows j/k with the drawer open and closes on Escape", () => {
    renderRun()
    fireEvent.click(main("p-riley"))
    key("j")
    expect(drawerName()).toBe("Avery Golf")
    expect(document.querySelector("[data-focus='true']")?.getAttribute("data-person-id")).toBe("p-avery")
    key("k")
    expect(drawerName()).toBe("Riley Foxtrot")
    key("Escape")
    expect(drawerOpen()).toBe("false")
  })

  it("scores the open candidate with a digit, then opens the next row; the last one closes", async () => {
    const { submit } = renderRun()
    fireEvent.click(main("p-morgan"))
    expect(bar().getByRole("button", { name: "4 Yes" }).getAttribute("aria-pressed")).toBe("false")
    key("4")
    expect(submit).toHaveBeenCalledWith({
      run_id: RUN_ID,
      person_id: "p-morgan",
      comment: "",
      human_judgment: { score: 4, scale: 5 },
    })
    expect(drawerName()).toBe("Casey Delta")
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Score Morgan Echo" }).textContent).toBe("Your score: 4/5"),
    )
    fireEvent.click(bar().getByRole("button", { name: "2 Lean no" }))
    expect(submit).toHaveBeenLastCalledWith(expect.objectContaining({ person_id: "p-casey" }))
    expect(drawerOpen()).toBe("false")
  })

  it("marks the saved score in the bar and ignores digits outside the rubric", async () => {
    const { submit } = renderRun()
    fireEvent.click(main("p-jordan"))
    key("5")
    key("k")
    expect(drawerName()).toBe("Jordan Bravo")
    await waitFor(() =>
      expect(bar().getByRole("button", { name: "5 Strong yes" }).getAttribute("aria-pressed")).toBe("true"),
    )
    key("9")
    expect(submit).toHaveBeenCalledTimes(1)
    expect(drawerName()).toBe("Jordan Bravo")
  })

  it("advances after a score saved in the dialog", () => {
    renderRun()
    fireEvent.click(main("p-jordan"))
    key("s")
    fireEvent.click(screen.getByRole("radio", { name: /^Score 3:/ }))
    fireEvent.click(screen.getByRole("button", { name: "Save" }))
    expect(drawerName()).toBe("Morgan Echo")
  })

  it("opens the tag editor with t and toggles the pin with p for the open candidate", async () => {
    const { saves } = renderRun()
    await tagsLoaded()
    fireEvent.click(main("p-jordan"))
    key("p")
    expect(
      (await screen.findByRole("button", { name: "Unpin Jordan Bravo" })).getAttribute("aria-pressed"),
    ).toBe("true")
    expect(bar().getByRole("button", { name: "Unpin P" }).getAttribute("aria-pressed")).toBe("true")
    await waitFor(() =>
      expect(saves.at(-1)).toEqual({ tags: ["Pinned"], assignments: { "p-jordan": ["Pinned"] } }),
    )
    key("t")
    expect(screen.getByRole("dialog", { name: "Tags for Jordan Bravo" })).toBeTruthy()
    // Keys inside the tag panel are its own: a digit there scores nobody.
    fireEvent.keyDown(screen.getByRole("textbox", { name: "Add tag" }), { key: "3" })
    expect(drawerName()).toBe("Jordan Bravo")
  })

  it("filters the table through the toolbar; the count reads the same rows", () => {
    renderRun()
    fireEvent.click(screen.getByRole("button", { name: "Overall score 5" }))
    expect(rowNames()).toEqual(["Jordan Bravo"])
    expect(tableHeadings()).toEqual(["Rating-based scores"])
    expect(screen.getByRole("status").textContent).toBe("1 of 6 results")
    fireEvent.click(screen.getByRole("button", { name: "Overall score 2" }))
    expect(rowNames()).toEqual(["Jordan Bravo", "Morgan Echo"])
    fireEvent.click(screen.getByRole("button", { name: "Add operator" }))
    fireEvent.click(screen.getByRole("checkbox", { name: /Drew Kilo/ }))
    expect(rowNames()).toEqual(["Jordan Bravo"])
    fireEvent.click(screen.getByRole("button", { name: "All scores" }))
    fireEvent.click(screen.getByRole("button", { name: "Remove Drew Kilo" }))
    expect(rowNames()).toEqual(RANKED_NAMES.flat())
    expect(screen.getByRole("status").textContent).toBe("6 results")
  })

  it("tags a row, pins another, and filters to the tagged people", async () => {
    const { saves } = renderRun()
    await tagsLoaded()
    fireEvent.click(screen.getByRole("button", { name: "Add tag to Jordan Bravo" }))
    const field = screen.getByRole("textbox", { name: "Add tag" })
    fireEvent.change(field, { target: { value: "Backend" } })
    fireEvent.keyDown(field, { key: "Enter" })
    fireEvent.keyDown(document, { key: "Escape" })
    expect((await row("p-jordan").findByText("Backend")).getAttribute("data-tag")).toBe("Backend")
    fireEvent.click(screen.getByRole("button", { name: "Pin Casey Delta" }))
    expect(
      (await screen.findByRole("button", { name: "Unpin Casey Delta" })).getAttribute("aria-pressed"),
    ).toBe("true")
    await waitFor(() =>
      expect(saves.at(-1)).toEqual({
        tags: ["Backend", "Pinned"],
        assignments: { "p-jordan": ["Backend"], "p-casey": ["Pinned"] },
      }),
    )
    fireEvent.click(await screen.findByRole("button", { name: "Tagged (2)" }))
    expect(rowNames()).toEqual(["Jordan Bravo", "Casey Delta"])
    fireEvent.click(screen.getByRole("button", { name: "Pinned" }))
    expect(rowNames()).toEqual(["Casey Delta"])
  })

  it("scores a person: the badge shows the saved score and the record goes to the queue", async () => {
    const { submit, onToast } = renderRun()
    fireEvent.click(screen.getByRole("button", { name: "Score Morgan Echo" }))
    fireEvent.click(screen.getByRole("radio", { name: /^Score 4:/ }))
    fireEvent.click(screen.getByRole("button", { name: "Save" }))
    expect(submit).toHaveBeenCalledWith({
      run_id: RUN_ID,
      person_id: "p-morgan",
      comment: "",
      human_judgment: { score: 4, scale: 5 },
    })
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Score Morgan Echo" }).textContent).toBe("Your score: 4/5"),
    )
    await waitFor(() => expect(onToast).toHaveBeenCalledWith({ message: "Sent." }))
  })

  it("shows a stopped queue's waiting records with Retry", () => {
    const waiting: FeedbackRecord = {
      run_id: RUN_ID,
      person_id: "p-morgan",
      comment: "",
      human_judgment: { score: 4, scale: 5 },
    }
    const { retry } = renderRun(null, { pending: [waiting], failure: "failed" })
    const header = within(screen.getByRole("banner"))
    expect(header.getByText("Saved on this device. 1 waiting to send.")).toBeTruthy()
    fireEvent.click(header.getByRole("button", { name: "Retry" }))
    expect(retry).toHaveBeenCalledTimes(1)
  })

  it("moves with j/k, opens the drawer with Enter, and presses the row's tag and score with t and s", async () => {
    renderRun()
    await tagsLoaded()
    key("j")
    key("j")
    expect(document.querySelector("[data-focus='true']")?.getAttribute("data-person-id")).toBe("p-avery")
    key("k")
    key("Enter")
    expect(drawerName()).toBe("Riley Foxtrot")
    expect(drawerOpen()).toBe("true")
    key("t")
    expect(screen.getByRole("dialog", { name: "Tags for Riley Foxtrot" })).toBeTruthy()
    fireEvent.keyDown(document, { key: "Escape" })
    key("s")
    expect(screen.getByRole("dialog", { name: "Score Riley Foxtrot" })).toBeTruthy()
  })
})
