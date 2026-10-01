// Layout for the decided pile's list under jsdom, which has none (and no ResizeObserver): the
// list's box, its rows' heights, how far it is scrolled, and a resize to announce. The
// virtualizer reads offset sizes and `scrollTop`; the list's own "near the end" check reads
// `scrollHeight`, `scrollTop` and `clientHeight`.

import { act, fireEvent } from "@testing-library/react"
import { vi } from "vitest"

/** A collapsed row's height, and an opened one's. */
export const ROW_PX = 61
export const OPEN_ROW_PX = 300

/** Every observer the page made, so a test can announce a resize to the ones watching. */
const observers = new Set<FakeResizeObserver>()

class FakeResizeObserver implements ResizeObserver {
  readonly watched = new Set<Element>()
  readonly announce: ResizeObserverCallback

  constructor(announce: ResizeObserverCallback) {
    this.announce = announce
    observers.add(this)
  }

  observe(target: Element): void {
    this.watched.add(target)
  }

  unobserve(target: Element): void {
    this.watched.delete(target)
  }

  disconnect(): void {
    this.watched.clear()
    observers.delete(this)
  }
}

const isList = (element: Element) => element.classList.contains("decision-list")

/** The list's content: the two spacers and the mounted rows between them. */
function contentHeight(list: Element): number {
  const parts = [...list.querySelectorAll<HTMLElement>(".decision-table > *")]
  const heights = parts.map((part) =>
    part.classList.contains("virtual-spacer") ? Number.parseFloat(part.style.height) : part.offsetHeight,
  )
  return heights.reduce((sum, height) => sum + height, 0)
}

function stub(property: string, read: (element: HTMLElement) => number): void {
  Object.defineProperty(HTMLElement.prototype, property, {
    configurable: true,
    get(this: HTMLElement) {
      return read(this)
    },
  })
}

/**
 * Stubs the layout for one test: a list `height` px tall, rows 61px (300px once opened).
 * `scrollTo` scrolls the list and `resize` announces an element's new size, as the browser
 * would. Call it before rendering; `vi.unstubAllGlobals()` takes the observer away.
 */
export function stubListLayout(height = 400) {
  const box = { height, scrollTop: 0 }
  observers.clear()
  vi.stubGlobal("ResizeObserver", FakeResizeObserver)
  stub("offsetWidth", () => 760)
  stub("offsetHeight", (element) => {
    if (!element.classList.contains("decision-row")) return box.height
    return element.hasAttribute("open") ? OPEN_ROW_PX : ROW_PX
  })
  stub("clientHeight", (element) => (isList(element) ? box.height : 0))
  stub("scrollHeight", (element) => (isList(element) ? contentHeight(element) : 0))
  stub("scrollTop", (element) => (isList(element) ? box.scrollTop : 0))

  function resize(element: Element): void {
    const entry: ResizeObserverEntry = {
      target: element,
      contentRect: element.getBoundingClientRect(),
      borderBoxSize: [],
      contentBoxSize: [],
      devicePixelContentBoxSize: [],
    }
    act(() => {
      for (const observer of observers) {
        if (observer.watched.has(element)) observer.announce([entry], observer)
      }
    })
  }

  return {
    /** The list's height; set it, then `resize(list)`. */
    box,
    resize,
    scrollTo(list: Element, top: number): void {
      box.scrollTop = top
      fireEvent.scroll(list)
    },
  }
}

export type ListLayout = ReturnType<typeof stubListLayout>
