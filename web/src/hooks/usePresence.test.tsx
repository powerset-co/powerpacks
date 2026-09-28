import { cleanup, fireEvent, render } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"

import { usePresence } from "./usePresence"

function stubMotion(reduced: boolean) {
  vi.stubGlobal("matchMedia", (media: string) => ({
    matches: reduced,
    media,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
  }))
}

function Overlay({ value }: { value: string | null }) {
  const { mounted, open, shown, onTransitionEnd } = usePresence(value)
  return mounted ? (
    <span data-overlay data-open={open} onTransitionEnd={onTransitionEnd}>
      {shown}
    </span>
  ) : null
}

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

describe("usePresence", () => {
  it("unmounts when the exit's opacity ends, showing the last value until then", () => {
    stubMotion(false)
    const { container, rerender } = render(<Overlay value="Clear filters" />)
    rerender(<Overlay value={null} />)
    const overlay = container.querySelector("[data-overlay]")
    if (!overlay) throw new Error("no overlay")
    expect(overlay.textContent).toBe("Clear filters")
    fireEvent.transitionEnd(overlay, { propertyName: "opacity" })
    expect(container.querySelector("[data-overlay]")).toBeNull()
  })

  it("stays through another property's transitionend (a press's translate)", () => {
    stubMotion(false)
    const { container, rerender } = render(<Overlay value="Clear filters" />)
    rerender(<Overlay value={null} />)
    const overlay = container.querySelector("[data-overlay]")
    if (!overlay) throw new Error("no overlay")
    fireEvent.transitionEnd(overlay, { propertyName: "transform" })
    expect(container.querySelector("[data-overlay]")).not.toBeNull()
  })
})
