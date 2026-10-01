import { cleanup, fireEvent, render } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"

import { motionMedia } from "@/testing/review-fixture"

import { ScrollRegion } from "./ScrollRegion"

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

describe("ScrollRegion", () => {
  /** A 400px box over `scrollHeight` of content, scrolled to `scrollTop`. */
  function layout(box: HTMLElement, scrollHeight: number, scrollTop = 0) {
    Object.defineProperty(box, "clientHeight", { configurable: true, value: 400 })
    Object.defineProperty(box, "scrollHeight", { configurable: true, value: scrollHeight })
    Object.defineProperty(box, "scrollTop", { configurable: true, value: scrollTop })
  }
  const nextFrame = () => new Promise((resolve) => requestAnimationFrame(resolve))

  function renderRegion(reduced: boolean) {
    vi.stubGlobal("matchMedia", motionMedia(reduced))
    const { container } = render(
      <ScrollRegion>
        <p>Body</p>
      </ScrollRegion>,
    )
    const box = container.querySelector<HTMLElement>(".identity-scroll")
    const cue = container.querySelector<HTMLButtonElement>(".scroll-cue")
    if (!box || !cue) throw new Error("the scroll region did not render")
    return { box, cue }
  }

  it("hides the cue while everything fits", async () => {
    const { cue } = renderRegion(true)
    await nextFrame()
    expect(cue.hidden).toBe(true)
    expect(cue.getAttribute("aria-label")).toBe("Scroll down")
  })

  it("shows the cue when content lands below the fold, and hides it at the end", async () => {
    const { box, cue } = renderRegion(true)
    layout(box, 900)
    box.append(document.createElement("p"))
    await vi.waitFor(() => expect(cue.hidden).toBe(false))
    layout(box, 900, 500)
    fireEvent.scroll(box)
    await vi.waitFor(() => expect(cue.hidden).toBe(true))
  })

  it("measures again on a window resize", async () => {
    const { box, cue } = renderRegion(true)
    layout(box, 900)
    fireEvent(window, new Event("resize"))
    await vi.waitFor(() => expect(cue.hidden).toBe(false))
  })

  it("scrolls by 70% of the box on a press, smoothly unless motion is reduced", async () => {
    const { box, cue } = renderRegion(false)
    const scrollBy = vi.fn()
    box.scrollBy = scrollBy
    layout(box, 900)
    fireEvent(window, new Event("resize"))
    await vi.waitFor(() => expect(cue.hidden).toBe(false))
    fireEvent.click(cue)
    expect(scrollBy).toHaveBeenCalledWith({ top: 280, behavior: "smooth" })
  })

  it("jumps under reduced motion", async () => {
    const { box, cue } = renderRegion(true)
    const scrollBy = vi.fn()
    box.scrollBy = scrollBy
    layout(box, 900)
    fireEvent(window, new Event("resize"))
    await vi.waitFor(() => expect(cue.hidden).toBe(false))
    fireEvent.click(cue)
    expect(scrollBy).toHaveBeenCalledWith({ top: 280, behavior: "auto" })
  })
})
