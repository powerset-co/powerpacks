import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it } from "vitest"

import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle, DialogTrigger } from "./dialog"

afterEach(cleanup)

function Harness() {
  return (
    <Dialog>
      <DialogTrigger>Rename search</DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Rename search</DialogTitle>
          <DialogDescription>Only the label changes.</DialogDescription>
        </DialogHeader>
      </DialogContent>
    </Dialog>
  )
}

describe("Dialog", () => {
  it("opens on its trigger, labelled by its title and described by its description", () => {
    render(<Harness />)
    expect(screen.queryByRole("dialog")).toBeNull()
    fireEvent.click(screen.getByRole("button", { name: "Rename search" }))
    const dialog = screen.getByRole("dialog", { name: "Rename search" })
    expect(dialog.getAttribute("data-state")).toBe("open")
    expect(dialog.getAttribute("aria-describedby")).toBe(screen.getByText("Only the label changes.").id)
  })

  it("closes on Escape", () => {
    render(<Harness />)
    fireEvent.click(screen.getByRole("button", { name: "Rename search" }))
    fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" })
    expect(screen.queryByRole("dialog")).toBeNull()
  })
})
