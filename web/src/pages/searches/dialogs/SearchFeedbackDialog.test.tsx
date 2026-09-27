import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"

import type { FeedbackOutcome, FeedbackRecord } from "@/lib/searches/feedback"

import { SearchFeedbackDialog } from "./SearchFeedbackDialog"

afterEach(cleanup)

const TRIGGER = "Send feedback about Staff engineer at Acme"

function setup(outcome: FeedbackOutcome = "sent") {
  const submit = vi.fn((_record: FeedbackRecord) => Promise.resolve(outcome))
  const onToast = vi.fn()
  render(
    <SearchFeedbackDialog
      runId="jordan-role"
      title="Staff engineer at Acme"
      submit={submit}
      onToast={onToast}
    />,
  )
  fireEvent.click(screen.getByRole("button", { name: TRIGGER }))
  return { submit, onToast }
}

describe("SearchFeedbackDialog", () => {
  it("opens on the notes with Send disabled until there is text", () => {
    setup()
    expect(screen.getByRole("dialog", { name: "Search feedback" })).toBeTruthy()
    const notes = screen.getByRole("textbox")
    expect(document.activeElement).toBe(notes)
    expect(screen.getByRole("button", { name: "Send" })).toHaveProperty("disabled", true)
    fireEvent.change(notes, { target: { value: "   " } })
    expect(screen.getByRole("button", { name: "Send" })).toHaveProperty("disabled", true)
  })

  it("sends the note as search feedback and toasts a queued outcome", async () => {
    const { submit, onToast } = setup("queued")
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "Too many recruiters" } })
    fireEvent.keyDown(screen.getByRole("textbox"), { key: "Enter", metaKey: true })
    expect(submit).toHaveBeenCalledWith({
      run_id: "jordan-role",
      person_id: "",
      comment: "Too many recruiters",
      human_judgment: null,
    })
    expect(screen.queryByRole("dialog")).toBeNull()
    await vi.waitFor(() => expect(onToast).toHaveBeenCalledWith({ message: "Saved to send later." }))
    expect(document.activeElement).toBe(screen.getByRole("button", { name: TRIGGER }))
  })

  it("closes on Escape and opens empty next time", () => {
    const { submit } = setup()
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "Draft" } })
    fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" })
    expect(screen.queryByRole("dialog")).toBeNull()
    fireEvent.click(screen.getByRole("button", { name: TRIGGER }))
    expect(screen.getByRole("textbox")).toHaveProperty("value", "")
    expect(submit).not.toHaveBeenCalled()
  })
})
