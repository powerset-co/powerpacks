import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { TagEditor, type TagEditorProps } from "./TagEditor"

function editor(props: Partial<TagEditorProps> = {}) {
  const handlers = { onToggle: vi.fn<(tag: string) => void>(), onRemove: vi.fn<(tag: string) => void>() }
  render(
    <TagEditor
      personName="Jordan Bravo"
      tags={["Backend", "Infra"]}
      applied={["Backend"]}
      disabled={false}
      {...handlers}
      {...props}
    />,
  )
  return handlers
}

function open() {
  fireEvent.click(screen.getByRole("button", { name: /Jordan Bravo$/ }))
  return screen.getByRole("textbox", { name: "Add tag" })
}

function stubMotion(reduced: boolean) {
  vi.stubGlobal("matchMedia", (media: string) => ({
    matches: reduced,
    media,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
  }))
}

beforeEach(() => stubMotion(true))
afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

describe("TagEditor", () => {
  it("names the trigger for adding or editing, and shows the person's tags", () => {
    editor()
    expect(screen.getByRole("button", { name: "Edit tags for Jordan Bravo" }).textContent).toBe("Backend")
    cleanup()
    editor({ applied: [] })
    expect(screen.getByRole("button", { name: "Add tag to Jordan Bravo" })).toBeDefined()
  })

  it("stays shut while the saved tags load", () => {
    editor({ disabled: true })
    const trigger = screen.getByRole("button", { name: "Edit tags for Jordan Bravo" })
    expect(trigger).toHaveProperty("disabled", true)
    fireEvent.click(trigger)
    expect(screen.queryByRole("textbox", { name: "Add tag" })).toBeNull()
  })

  it("keeps a removed chip through its exit, then drops it", () => {
    stubMotion(false)
    const { rerender } = render(
      <TagEditor
        personName="Casey Delta"
        tags={["Backend", "Infra"]}
        applied={["Backend", "Infra"]}
        disabled={false}
        onToggle={vi.fn()}
        onRemove={vi.fn()}
      />,
    )
    rerender(
      <TagEditor
        personName="Casey Delta"
        tags={["Backend", "Infra"]}
        applied={["Backend"]}
        disabled={false}
        onToggle={vi.fn()}
        onRemove={vi.fn()}
      />,
    )
    const leaving = document.querySelector<HTMLElement>("[data-tag='Infra']")
    expect(leaving?.dataset.open).toBe("false")
    fireEvent.transitionEnd(leaving ?? document.body)
    expect(document.querySelector("[data-tag='Infra']")).toBeNull()
    expect(document.querySelector("[data-tag='Backend']")?.getAttribute("data-open")).toBe("true")
  })

  it("stays open through a scroll that leaves its row in place, and closes once the row moves", () => {
    editor()
    open()
    fireEvent.scroll(window)
    expect(screen.getByRole("textbox", { name: "Add tag" })).toBeDefined()
    const trigger = screen.getByRole("button", { name: "Edit tags for Jordan Bravo" })
    vi.spyOn(trigger, "getBoundingClientRect").mockReturnValue(new DOMRect(0, -40, 28, 28))
    fireEvent.scroll(window)
    expect(screen.queryByRole("textbox", { name: "Add tag" })).toBeNull()
  })

  it("opens with the field focused and each tag as a toggle", () => {
    editor()
    const field = open()
    expect(document.activeElement).toBe(field)
    expect(screen.getByRole("button", { name: /Backend/, pressed: true })).toBeDefined()
    fireEvent.click(screen.getByRole("button", { name: /Infra/, pressed: false }))
    expect(field).toHaveProperty("value", "")
  })

  it("toggles a tag", () => {
    const { onToggle } = editor()
    open()
    fireEvent.click(screen.getByRole("button", { name: /Infra/, pressed: false }))
    expect(onToggle).toHaveBeenCalledWith("Infra")
  })

  it("adds a new tag on Enter, or the search's tag typed in another case", () => {
    const { onToggle } = editor()
    const field = open()
    fireEvent.change(field, { target: { value: "  Payments " } })
    expect(screen.getByRole("button", { name: "Create “Payments”" })).toBeDefined()
    fireEvent.keyDown(field, { key: "Enter" })
    expect(onToggle).toHaveBeenLastCalledWith("Payments")
    fireEvent.change(field, { target: { value: "INFRA" } })
    fireEvent.keyDown(field, { key: "Enter" })
    expect(onToggle).toHaveBeenLastCalledWith("Infra")
  })

  it("does not create a tag the search has in another case", () => {
    editor()
    const field = open()
    fireEvent.change(field, { target: { value: "backend" } })
    expect(screen.queryByRole("button", { name: /Create/ })).toBeNull()
  })

  it("removes a tag from the search", () => {
    const { onRemove } = editor()
    open()
    fireEvent.click(screen.getByRole("button", { name: "Remove Infra from search" }))
    expect(onRemove).toHaveBeenCalledWith("Infra")
  })

  it("says when there is nothing to pick", () => {
    editor({ tags: [], applied: [] })
    open()
    expect(screen.getByText("No tags yet. Type to create one.")).toBeDefined()
  })

  it("closes on Escape and gives focus back to the trigger", () => {
    editor()
    const field = open()
    fireEvent.keyDown(field, { key: "Escape" })
    expect(screen.queryByRole("dialog")).toBeNull()
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "Edit tags for Jordan Bravo" }))
  })

  it("closes on a press outside", () => {
    editor()
    open()
    fireEvent.pointerDown(document.body)
    expect(screen.queryByRole("dialog")).toBeNull()
  })
})
