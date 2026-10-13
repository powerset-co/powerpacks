import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import { afterEach, expect, it, vi } from "vitest"

import { decodePeople } from "@/lib/api/people"
import { must } from "@/lib/must"
import { PAYLOAD } from "@/testing/people-fixture"

import { PersonFeedback } from "./PersonFeedback"

const api = vi.hoisted(() => ({ sendPersonFeedback: vi.fn() }))
vi.mock("@/lib/api/feedback", () => api)

const row = must(decodePeople(PAYLOAD)[0])

afterEach(() => {
  cleanup()
  vi.resetAllMocks()
})

function write(note: string) {
  render(<PersonFeedback row={row} />)
  fireEvent.click(screen.getByRole("button", { name: "Send feedback about Jordan Bravo" }))
  fireEvent.change(screen.getByRole("textbox", { name: "Notes" }), { target: { value: note } })
  fireEvent.click(screen.getByRole("button", { name: "Send" }))
}

it("sends a note about the person from the drawer's flag", async () => {
  api.sendPersonFeedback.mockResolvedValue(undefined)
  write("Should be shared.")
  expect(await screen.findByRole("status")).toHaveProperty("textContent", "Sent. Thank you.")
  expect(api.sendPersonFeedback).toHaveBeenCalledWith("Should be shared.", row)
})

it("keeps the note and says why when it did not go out", async () => {
  api.sendPersonFeedback.mockRejectedValue(new Error("Sign in to Powerset again, then send."))
  write("Should be shared.")
  expect(await screen.findByRole("alert")).toHaveProperty(
    "textContent",
    "Sign in to Powerset again, then send.",
  )
  expect(screen.getByRole("textbox", { name: "Notes" })).toHaveProperty("value", "Should be shared.")
})
