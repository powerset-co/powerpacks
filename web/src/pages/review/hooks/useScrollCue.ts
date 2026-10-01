import { useCallback, useEffect, useRef, useState } from "react"

import { useReducedMotion } from "@/hooks/useReducedMotion"
import { moreBelow, scrollStep } from "@/lib/review/scroll"

/**
 * The "scroll down" cue for a box that scrolls inside the page. Put `scroller` on the box:
 * `more` holds while content sits below its fold, `scrollDown` is a press of the cue. It
 * measures again on scroll, on a window resize and whenever the box's content changes (show
 * more, the dossier landing); `refresh` is for a change outside the box (a row opening around it).
 */
export function useScrollCue<Box extends HTMLElement>() {
  const scroller = useRef<Box>(null)
  const reducedMotion = useReducedMotion()
  const [more, setMore] = useState(false)
  const frame = useRef(0)

  const refresh = useCallback(() => {
    if (frame.current) return
    frame.current = window.requestAnimationFrame(() => {
      frame.current = 0
      if (scroller.current) setMore(moreBelow(scroller.current))
    })
  }, [])

  useEffect(() => {
    const box = scroller.current
    if (!box) return
    refresh()
    box.addEventListener("scroll", refresh, { passive: true })
    window.addEventListener("resize", refresh)
    const content = new MutationObserver(refresh)
    content.observe(box, { childList: true, subtree: true, characterData: true, attributes: true })
    return () => {
      box.removeEventListener("scroll", refresh)
      window.removeEventListener("resize", refresh)
      content.disconnect()
      window.cancelAnimationFrame(frame.current)
      frame.current = 0
    }
  }, [refresh])

  const scrollDown = useCallback(() => {
    const box = scroller.current
    box?.scrollBy({ top: scrollStep(box.clientHeight), behavior: reducedMotion ? "auto" : "smooth" })
  }, [reducedMotion])

  return { scroller, more, scrollDown, refresh }
}
