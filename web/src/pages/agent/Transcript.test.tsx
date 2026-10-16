import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react"
import { afterEach, expect, it, vi } from "vitest"

import type { Entry } from "@/types/agent"

import { Transcript } from "./Transcript"

const store = vi.hoisted(() => ({ send: vi.fn() }))
vi.mock("@/lib/agent/store", () => store)

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.useRealTimers()
  store.send.mockReset()
})

it("copies a message's text and says Copied for a moment", async () => {
  vi.useFakeTimers()
  const writeText = vi.fn(() => Promise.resolve())
  vi.stubGlobal("navigator", { clipboard: { writeText } })
  render(
    <Transcript
      entries={[
        { kind: "user", id: "u1", text: "Who do I know at Stripe?" },
        { kind: "agent", id: "a1", text: "**Two people.**\n\n- Jordan Bravo" },
      ]}
      running={false}
    />,
  )
  const buttons = screen.getAllByRole("button", { name: "Copy message" })
  const answer = buttons.at(-1)
  if (buttons.length !== 2 || !answer) throw new Error("Expected a copy button per message")
  await act(async () => {
    fireEvent.click(answer)
    await Promise.resolve()
  })
  expect(writeText).toHaveBeenCalledWith("**Two people.**\n\n- Jordan Bravo")
  expect(screen.getByRole("button", { name: "Copied" })).toBeTruthy()
  act(() => {
    vi.advanceTimersByTime(1500)
  })
  expect(screen.queryByRole("button", { name: "Copied" })).toBeNull()
  expect(screen.getAllByRole("button", { name: "Copy message" })).toHaveLength(2)
})

it("folds a turn's work into one line that opens on a click, with the shell wrapper gone", () => {
  render(
    <Transcript
      entries={[
        { kind: "user", id: "u1", text: "Find a systems engineer" },
        {
          kind: "command",
          id: "c1",
          command: "/bin/zsh -lc 'cat /Users/casey/.powerpacks/app/packs/search/skills/search/SKILL.md'",
          status: "done",
          exitCode: 0,
          output: "# Search",
          durationMs: 40,
        },
        {
          kind: "command",
          id: "c2",
          command: "/bin/zsh -lc \"uv run --project . python - <<'PY'\nimport json\nPY\"",
          status: "done",
          exitCode: 0,
          output: "",
          durationMs: 4400,
        },
        { kind: "files", id: "f1", paths: ["a.json", "b.json"], status: "done" },
        { kind: "agent", id: "a1", text: "Here they are." },
      ]}
      running={false}
    />,
  )
  const work = screen.getByRole("list", { name: "Work" }).closest("details")
  if (!work) throw new Error("Expected the work to fold")
  expect(work.open).toBe(false)
  expect(work.querySelector("summary")?.textContent).toBe("Ran 2 commands, edited 2 files · 4.4s")
  expect(screen.getByText("cat ~/.powerpacks/app/packs/search/skills/search/SKILL.md")).toBeTruthy()
  expect(screen.getByText("uv run --project . python - <<'PY'")).toBeTruthy()
})

const PREPARE =
  "/bin/zsh -lc \"uv run --env-file .env --project . python packs/search/primitives/search_network_pipeline/search_network_pipeline.py prepare --backend local --query 'VP of finance in New York' --output-dir .powerpacks/search/vp-finance-ny\""

// What prepare prints (emit: indent=2), after whatever uv said first.
function prepared(id: string, printed: Record<string, unknown>): Entry {
  const output = `warning: uv is resolving\n${JSON.stringify(printed, null, 2)}\n`
  return { kind: "command", id, command: PREPARE, status: "done", exitCode: 0, output, durationMs: 3900 }
}

const PREVIEW = {
  query: "VP of finance in New York",
  status: "preview_ready",
  preview: {
    filters: {
      metro_areas: ["New York Metropolitan Area"],
      seniority_bands: ["vice-president", "director"],
      cities: [],
      is_current_role: true,
    },
    traits: [{ meaning: "role", temporal: "current", value: "VP of Finance" }],
    pool_estimate: { status: "completed", matched_people: 1234, matched_positions: 1500, total_people: 5000 },
    runtime_notes: ["age_min 30 filters on LLM-inferred birth year", "broad search: consider narrowing"],
    role_title_intent: { bm25_queries: ["vp finance"] },
  },
}

it("shows a prepared search as a card in its command's place, its traits and filters editable; Execute sends execute, Modify focuses the box", () => {
  render(
    <>
      <Transcript
        entries={[
          { kind: "user", id: "u1", text: "VP of finance in New York" },
          prepared("c1", PREVIEW),
          { kind: "agent", id: "a1", text: "Search prepared. Execute or modify?" },
        ]}
        running={false}
      />
      <textarea id="agent-message" aria-label="Message" />
    </>,
  )
  expect(screen.queryByRole("list", { name: "Work" })).toBeNull()
  const card = screen.getByRole("region", { name: "Search" })
  expect(within(card).getByRole("heading").textContent).toBe("VP of finance in New York")
  const lines = (name: string) =>
    within(within(card).getByRole("list", { name }))
      .getAllByRole("listitem")
      .map((item) => item.textContent)
  const fields = (name: string) =>
    within(within(card).getByRole("list", { name }))
      .getAllByRole<HTMLInputElement>("textbox")
      .map((field) => field.value)
  expect(fields("Traits")).toEqual(["VP of Finance"])
  expect(fields("Filters")).toEqual(["New York Metropolitan Area", "vice-president, director", "true"])
  expect(within(card).getByText("Pool: 1,234 of 5,000 people")).toBeTruthy()
  expect(lines("Notes")).toEqual([
    "age_min 30 filters on LLM-inferred birth year",
    "broad search: consider narrowing",
  ])

  fireEvent.click(within(card).getByRole("button", { name: "Execute" }))
  expect(store.send).toHaveBeenCalledWith("execute")
  fireEvent.click(within(card).getByRole("button", { name: "Modify" }))
  expect(document.activeElement).toBe(screen.getByRole("textbox", { name: "Message" }))
})

it("asks only on the newest card while the agent waits; the company fast path is no card", () => {
  const entries: Entry[] = [
    prepared("c0", {
      query: "people at Stripe",
      status: "company_directory_fast_path",
      tool: "list_company_people",
    }),
    prepared("c1", PREVIEW),
    { kind: "user", id: "u1", text: "only New York City" },
    prepared("c2", { ...PREVIEW, query: "VP of finance in New York City" }),
  ]
  const { rerender } = render(<Transcript entries={entries} running={false} />)
  const cards = screen.getAllByRole("region", { name: "Search" })
  const [older, newest] = cards
  if (cards.length !== 2 || !older || !newest) throw new Error("Expected two search cards")
  expect(within(older).queryByRole("button")).toBeNull()
  expect(within(older).queryByRole("textbox")).toBeNull()
  expect(within(newest).getByRole("button", { name: "Execute" })).toBeTruthy()
  expect(within(newest).getByRole("button", { name: "Modify" })).toBeTruthy()
  expect(screen.getByRole("list", { name: "Work" }).textContent).toContain(
    "search_network_pipeline.py prepare",
  )

  rerender(<Transcript entries={entries} running={true} />)
  expect(screen.queryByRole("button", { name: "Execute" })).toBeNull()
})

it("never offers a search again once a command has run after its card", () => {
  const ran: Entry = {
    kind: "command",
    id: "c2",
    command: "uv run python search_network_pipeline.py run --ledger pipeline.ledger.json --execute-approved",
    status: "done",
    exitCode: 0,
    output: "{}",
    durationMs: 26000,
  }
  render(
    <Transcript
      entries={[
        prepared("c1", PREVIEW),
        { kind: "user", id: "u1", text: "execute" },
        ran,
        { kind: "agent", id: "a1", text: "207 found." },
      ]}
      running={false}
    />,
  )
  expect(screen.getByRole("region", { name: "Search" })).toBeTruthy()
  expect(screen.queryByRole("button", { name: "Execute" })).toBeNull()
  expect(screen.queryByRole("button", { name: "Modify" })).toBeNull()
})

it("shows a blocked search's blockers under its notes and offers only Modify", () => {
  render(
    <Transcript
      entries={[
        prepared("c1", {
          query: "VP of finance in New York",
          status: "blocked_user_action",
          preview: {
            filters: { metro_areas: ["New York Metropolitan Area"] },
            runtime_blockers: ["role_search_filters has no semantic_query", "bm25_queries is empty"],
          },
        }),
      ]}
      running={false}
    />,
  )
  const card = screen.getByRole("region", { name: "Search" })
  const blockers = within(card).getByRole("list", { name: "Blockers" })
  expect(
    within(blockers)
      .getAllByRole("listitem")
      .map((item) => item.textContent),
  ).toEqual(["role_search_filters has no semantic_query", "bm25_queries is empty"])
  expect(within(card).queryByRole("list", { name: "Traits" })).toBeNull()
  expect(within(card).queryByRole("button", { name: "Execute" })).toBeNull()
  expect(within(card).getByRole("button", { name: "Modify" })).toBeTruthy()
})
