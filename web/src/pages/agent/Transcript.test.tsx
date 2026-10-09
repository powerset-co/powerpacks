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
