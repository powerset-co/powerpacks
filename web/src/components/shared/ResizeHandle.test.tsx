import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import { afterEach, expect, it, vi } from "vitest"

import { ResizeHandle } from "./ResizeHandle"

afterEach(cleanup)

function handle(edge: "left" | "right") {
  const calls = { onDrag: vi.fn(), onStep: vi.fn(), onReset: vi.fn() }
  render(<ResizeHandle label="Resize panel" edge={edge} {...calls} />)
  return { ...calls, element: screen.getByRole("button", { name: "Resize panel" }) }
}

it("grows the panel toward its handle's side with the arrow keys", () => {
  const right = handle("right")
  fireEvent.keyDown(right.element, { key: "ArrowRight" })
  fireEvent.keyDown(right.element, { key: "ArrowLeft" })
  expect(right.onStep.mock.calls).toEqual([[1], [-1]])
  cleanup()
  const left = handle("left")
  fireEvent.keyDown(left.element, { key: "ArrowLeft" })
  expect(left.onStep.mock.calls).toEqual([[1]])
})

it("follows the pointer only while it is held, and resets on double-click", () => {
  const { element, onDrag, onReset } = handle("left")
  // jsdom has no pointer capture.
  element.setPointerCapture = vi.fn()
  element.hasPointerCapture = () => false
  fireEvent.pointerMove(element, { clientX: 500 })
  expect(onDrag).not.toHaveBeenCalled()
  fireEvent.pointerDown(element, { button: 0, pointerId: 1, clientX: 600 })
  expect(document.documentElement.hasAttribute("data-resizing")).toBe(true)
  fireEvent.pointerMove(element, { clientX: 580 })
  fireEvent.pointerUp(element, { pointerId: 1 })
  fireEvent.pointerMove(element, { clientX: 400 })
  expect(onDrag.mock.calls).toEqual([[580]])
  expect(document.documentElement.hasAttribute("data-resizing")).toBe(false)
  fireEvent.doubleClick(element)
  expect(onReset).toHaveBeenCalledOnce()
})
