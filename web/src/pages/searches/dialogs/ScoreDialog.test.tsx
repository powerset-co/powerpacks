import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"

import type { FeedbackOutcome, FeedbackRecord } from "@/lib/searches/feedback"

import { CASEY, RATINGS } from "./fixture"
import { ScoreDialog } from "./ScoreDialog"

afterEach(cleanup)

function setup(score: number | null = null, note = "") {
  const onSaved = vi.fn((_record: FeedbackRecord) => undefined)
  const submit = vi.fn((_record: FeedbackRecord) => Promise.resolve<FeedbackOutcome>("sent"))
  const onToast = vi.fn()
  render(
    <ScoreDialog
      runId="jordan-role"
      candidate={CASEY}
      rubric={RATINGS.rubric}
      score={score}
      note={note}
      onSaved={onSaved}
      submit={submit}
      onToast={onToast}
    />,
  )
  fireEvent.click(screen.getByRole("button", { name: "Score Casey Delta" }))
  return { onSaved, submit, onToast }
}

function choice(score: number): HTMLInputElement {
  const input = screen.getByRole("radio", { name: new RegExp(`^Score ${score}:`) })
  if (!(input instanceof HTMLInputElement)) throw new Error("not an input")
  return input
}

describe("ScoreDialog", () => {
  it("opens with five choices, the context line, and Save disabled", () => {
    setup()
    expect(screen.getByRole("dialog", { name: "Score Casey Delta" })).toBeTruthy()
    expect(screen.getByText("Staff engineer · Acme")).toBeTruthy()
    expect(screen.getAllByRole("radio")).toHaveLength(5)
    expect(screen.getByRole("radio", { name: "Score 5: Strong yes — Particularly compelling" })).toBeTruthy()
    expect(screen.getByRole("button", { name: "Save" })).toHaveProperty("disabled", true)
    expect(document.activeElement).toBe(choice(1))
  })

  it("preselects the saved score and note", () => {
    setup(4, "Relevant experience")
    expect(choice(4).checked).toBe(true)
    expect(document.activeElement).toBe(choice(4))
    expect(screen.getByRole("textbox")).toHaveProperty("value", "Relevant experience")
    expect(screen.getByText("Yes")).toBeTruthy()
  })

  it("saves the chosen score and note, closes, and toasts the outcome", async () => {
    const { onSaved, submit, onToast } = setup()
    fireEvent.click(choice(5))
    fireEvent.change(screen.getByRole("textbox"), { target: { value: " Relevant experience " } })
    fireEvent.click(screen.getByRole("button", { name: "Save" }))
    const record: FeedbackRecord = {
      run_id: "jordan-role",
      person_id: "casey",
      comment: "Relevant experience",
      human_judgment: { score: 5, scale: 5 },
    }
    expect(onSaved).toHaveBeenCalledWith(record)
    expect(submit).toHaveBeenCalledWith(record)
    expect(screen.queryByRole("dialog")).toBeNull()
    await vi.waitFor(() => expect(onToast).toHaveBeenCalledWith({ message: "Sent." }))
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "Score Casey Delta" }))
  })

  it("chooses with 1–5 and saves with Enter", () => {
    const { onSaved } = setup()
    fireEvent.keyDown(choice(1), { key: "3" })
    expect(choice(3).checked).toBe(true)
    expect(document.activeElement).toBe(choice(3))
    fireEvent.keyDown(choice(3), { key: "Enter" })
    expect(onSaved.mock.calls[0]?.[0].human_judgment).toEqual({ score: 3, scale: 5 })
  })

  it("keeps Enter in the notes and saves on Ctrl + Enter", () => {
    const { onSaved } = setup(2)
    const notes = screen.getByRole("textbox")
    fireEvent.keyDown(notes, { key: "Enter" })
    fireEvent.keyDown(notes, { key: "3" })
    expect(onSaved).not.toHaveBeenCalled()
    expect(choice(2).checked).toBe(true)
    fireEvent.keyDown(notes, { key: "Enter", ctrlKey: true })
    expect(onSaved.mock.calls[0]?.[0].human_judgment).toEqual({ score: 2, scale: 5 })
  })

  it("closes on Escape without saving and returns focus to the badge", async () => {
    const { onSaved } = setup(4)
    fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" })
    expect(screen.queryByRole("dialog")).toBeNull()
    expect(onSaved).not.toHaveBeenCalled()
    await vi.waitFor(() =>
      expect(document.activeElement).toBe(screen.getByRole("button", { name: "Score Casey Delta" })),
    )
  })

  it("labels the badge with the score", () => {
    setup(4)
    fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" })
    expect(screen.getByRole("button", { name: "Score Casey Delta" }).textContent).toBe("Your score: 4/5")
  })
})
