import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"

import type { FeedbackOutcome } from "@/lib/searches/feedback"
import type { FeedbackRecord } from "@/types/searches"

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
    await vi.waitFor(() => expect(onToast).toHaveBeenCalledWith({ message: "Saved on this device." }))
    expect(document.activeElement).toBe(screen.getByRole("button", { name: TRIGGER }))
  })

  it("sends a candidate's note with their id from the drawer's flag", () => {
    const submit = vi.fn((_record: FeedbackRecord) => Promise.resolve<FeedbackOutcome>("sent"))
    render(
      <SearchFeedbackDialog
        runId="jordan-role"
        title="Staff Engineer · Example Labs"
        candidate={{ person_id: "p-jordan", name: "Jordan Bravo" }}
        submit={submit}
        onToast={vi.fn()}
      />,
    )
    fireEvent.click(screen.getByRole("button", { name: "Send feedback about Jordan Bravo" }))
    expect(screen.getByRole("dialog", { name: "Candidate feedback" })).toBeTruthy()
    expect(screen.getByText("Jordan Bravo · Staff Engineer · Example Labs")).toBeTruthy()
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "Too junior" } })
    fireEvent.click(screen.getByRole("button", { name: "Send" }))
    expect(submit).toHaveBeenCalledWith({
      run_id: "jordan-role",
      person_id: "p-jordan",
      comment: "Too junior",
      human_judgment: null,
    })
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
