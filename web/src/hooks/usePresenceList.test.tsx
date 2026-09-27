import { cleanup, fireEvent, render, renderHook } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"

import { usePresenceList } from "./usePresenceList"

interface Held {
  key: string
  value: string
}

function stubMotion(reduced: boolean) {
  vi.stubGlobal("matchMedia", (media: string) => ({
    matches: reduced,
    media,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
  }))
}

const keyOf = (held: Held) => held.key
const held = (...keys: string[]): Held[] => keys.map((key) => ({ key, value: key.toUpperCase() }))

// Each entry as a span, so a transitionend can be fired on a leaving one.
function Chips({ items }: { items: Held[] }) {
  const entries = usePresenceList(items, keyOf)
  return (
    <>
      {entries.map(({ key, open, onTransitionEnd }) => (
        <span key={key} data-key={key} data-open={open} onTransitionEnd={onTransitionEnd} />
      ))}
    </>
  )
}

const keys = (container: HTMLElement) =>
  [...container.querySelectorAll<HTMLElement>("[data-key]")].map((el) => [el.dataset.key, el.dataset.open])

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

describe("usePresenceList", () => {
  it("keeps a removed item, closed, until its transition ends", () => {
    stubMotion(false)
    const { container, rerender } = render(<Chips items={held("a", "b")} />)
    expect(keys(container)).toEqual([
      ["a", "true"],
      ["b", "true"],
    ])
    rerender(<Chips items={held("b")} />)
    expect(keys(container)).toEqual([
      ["a", "false"],
      ["b", "true"],
    ])
    const leaving = container.querySelector("[data-key='a']")
    if (!leaving) throw new Error("no leaving entry")
    fireEvent.transitionEnd(leaving)
    expect(keys(container)).toEqual([["b", "true"]])
  })

  it("reads items fresh by key without re-rendering forever", () => {
    stubMotion(false)
    const { result, rerender } = renderHook(({ items }) => usePresenceList(items, keyOf), {
      initialProps: { items: held("a") },
    })
    // New objects for the same keys, as a parent re-render produces.
    rerender({ items: [{ key: "a", value: "changed" }] })
    expect(result.current[0]?.item.value).toBe("changed")
  })

  it("drops removed items at once under reduced motion", () => {
    stubMotion(true)
    const { result, rerender } = renderHook(({ items }) => usePresenceList(items, keyOf), {
      initialProps: { items: held("a", "b") },
    })
    rerender({ items: held("b") })
    expect(result.current.map((entry) => entry.key)).toEqual(["b"])
  })
})
