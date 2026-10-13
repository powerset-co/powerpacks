import { useCallback, useEffect, useLayoutEffect, useRef, useState, type CSSProperties } from "react"

import { type DismissBy, useDismiss } from "@/hooks/useDismiss"

const GAP = 6
const EDGE = 8

interface Placement {
  style: CSSProperties
  // Where the anchor's foot was: a scroll that moves it closes the panel.
  anchorBottom: number
}

const NONE = () => undefined

function below(anchor: HTMLElement, width: number): Placement {
  const box = anchor.getBoundingClientRect()
  return {
    style: {
      top: box.bottom + GAP,
      left: Math.max(EDGE, Math.min(box.left, window.innerWidth - width - EDGE)),
      width,
    },
    anchorBottom: box.bottom,
  }
}

/**
 * A fixed panel under a trigger, kept inside the window and moved above the trigger when it
 * would run off the bottom. Portalled to the body: a virtual row clips and recycles its cells.
 * Put `anchor` on the trigger and `panel` on the panel, and give the panel `place` as its
 * style. Escape (which hands focus back to the trigger), a press outside, or a scroll that
 * moves the anchor closes it and tells `onDismiss` which; `hide` is the owner's own close.
 */
export function useFloatPanel(width: number, onDismiss: (by: DismissBy) => void = NONE) {
  const anchor = useRef<HTMLButtonElement>(null)
  const panel = useRef<HTMLDivElement>(null)
  const [placed, setPlaced] = useState<Placement | null>(null)

  const show = useCallback(() => {
    if (anchor.current) setPlaced(below(anchor.current, width))
  }, [width])
  const hide = useCallback(() => setPlaced(null), [])
  const dismiss = useCallback(
    (by: DismissBy) => {
      setPlaced(null)
      if (by === "escape") anchor.current?.focus()
      onDismiss(by)
    },
    [onDismiss],
  )
  useDismiss(placed !== null, panel, anchor, dismiss)

  // The panel's height is known only once it is in the DOM: one that would run off the bottom
  // moves above the anchor before paint.
  useLayoutEffect(() => {
    const box = panel.current
    if (!placed || !box || !anchor.current) return
    const height = box.offsetHeight
    if (typeof placed.style.top !== "number" || placed.style.top + height + EDGE <= window.innerHeight) return
    const above = anchor.current.getBoundingClientRect().top - GAP - height
    box.style.top = `${String(Math.max(EDGE, above))}px`
  }, [placed])

  useEffect(() => {
    if (!placed) return
    // A scroll inside the panel, or one that leaves the anchor where it was, keeps it open.
    const onScroll = (event: Event) => {
      if (event.target instanceof Node && panel.current?.contains(event.target)) return
      if (anchor.current?.getBoundingClientRect().bottom === placed.anchorBottom) return
      dismiss("outside")
    }
    window.addEventListener("scroll", onScroll, true)
    return () => window.removeEventListener("scroll", onScroll, true)
  }, [placed, dismiss])

  return { anchor, panel, place: placed?.style ?? null, open: placed !== null, show, hide }
}
