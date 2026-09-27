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

// The run as RunPane reads it: from the query cache, so a saved score shows through it.
function Harness({ feedback, onToast }: { feedback: Feedback; onToast: () => void }) {
  const run = useSearchRun(RUN_ID)
  return run.data ? (
    <RunView payload={run.data} status="completed" feedback={feedback} onToast={onToast} />
  ) : null
}

function renderRun(tags: Tagged | null = null) {
  const saves = tagServer(tags)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  client.setQueryData(searchRunKey(RUN_ID), RUN)
  const submit = vi.fn((_record: FeedbackRecord) => Promise.resolve<FeedbackOutcome>("sent"))
  const feedback: Feedback = { pending: [], submit, retry: () => Promise.resolve() }
  const onToast = vi.fn()
  render(
    <QueryClientProvider client={client}>
      <Harness feedback={feedback} onToast={onToast} />
    </QueryClientProvider>,
  )
  return { saves, submit, onToast }
}

const rowNames = () =>
  [...document.querySelectorAll<HTMLElement>(".result-row .result-who b")].map((name) => name.textContent)

function row(personId: string) {
  return within(document.querySelector<HTMLElement>(`[data-person-id='${personId}']`) ?? document.body)
}

// The row's own toggle: the person's line, outside the actions.
function main(personId: string): HTMLElement {
  const button = document.querySelector<HTMLElement>(`[data-person-id='${personId}'] .result-main`)
  if (!button) throw new Error(`no row for ${personId}`)
  return button
}

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
    expect(screen.getAllByRole("heading", { level: 3 }).map((heading) => heading.textContent)).toEqual([
      "Jev qualification scores",
      "Rating-based scores",
    ])
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

  it("expands a row in place to its evidence; each row on its own", () => {
    renderRun()
    const jordan = main("p-jordan")
    fireEvent.click(jordan)
    expect(jordan.getAttribute("aria-expanded")).toBe("true")
    const evidence = within(document.querySelector<HTMLElement>("[data-evidence]") ?? document.body)
    expect(evidence.getByText("Built this exact system twice.")).toBeTruthy()
    expect(evidence.getByText("Example University")).toBeTruthy()
    fireEvent.click(main("p-casey"))
    expect(document.querySelectorAll("[data-evidence]")).toHaveLength(2)
    fireEvent.click(jordan)
    expect(document.querySelectorAll("[data-evidence]")).toHaveLength(1)
  })

  it("filters the table through the toolbar; the count reads the same rows", () => {
    renderRun()
    fireEvent.click(screen.getByRole("button", { name: "Overall score 5" }))
    expect(rowNames()).toEqual(["Jordan Bravo"])
    expect(screen.getAllByRole("heading", { level: 3 }).map((heading) => heading.textContent)).toEqual([
      "Rating-based scores",
    ])
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

  it("moves with j/k, opens with Enter, and presses the row's tag and score with t and s", async () => {
    renderRun()
    await tagsLoaded()
    const key = (name: string) => act(() => void fireEvent.keyDown(document.body, { key: name }))
    key("j")
    key("j")
    expect(document.querySelector("[data-focus='true']")?.getAttribute("data-person-id")).toBe("p-avery")
    key("k")
    key("Enter")
    expect(main("p-riley").getAttribute("aria-expanded")).toBe("true")
    key("t")
    expect(screen.getByRole("dialog", { name: "Tags for Riley Foxtrot" })).toBeTruthy()
    fireEvent.keyDown(document, { key: "Escape" })
    key("s")
    expect(screen.getByRole("dialog", { name: "Score Riley Foxtrot" })).toBeTruthy()
  })
})
