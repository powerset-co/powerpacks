import { act, cleanup, fireEvent, render, screen } from "@testing-library/react"
import { afterEach, expect, it, vi } from "vitest"

import { Transcript } from "./Transcript"

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.useRealTimers()
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
    />,
  )
  const work = screen.getByRole("list", { name: "Work" }).closest("details")
  if (!work) throw new Error("Expected the work to fold")
  expect(work.open).toBe(false)
  expect(work.querySelector("summary")?.textContent).toBe("Ran 2 commands, edited 2 files · 4.4s")
  expect(screen.getByText("cat ~/.powerpacks/app/packs/search/skills/search/SKILL.md")).toBeTruthy()
  expect(screen.getByText("uv run --project . python - <<'PY'")).toBeTruthy()
})
