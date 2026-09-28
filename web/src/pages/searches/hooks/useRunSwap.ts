import { useEffect, useLayoutEffect, useRef, useState, type RefObject } from "react"

import { useReducedMotion } from "@/hooks/useReducedMotion"
import { cssMs } from "@/lib/motion"
import { must } from "@/lib/must"

/**
 * Which run the main pane shows while the URL switches runs. Two runs never show at once:
 * the outgoing pane fades to zero (--t-fast, --ease-in) and stays there; when the fade ends,
 * the pane is keyed to the newest picked run, which fades in (`.run-pane` on mount, --t-med).
 * A pick made mid-fade is simply the run shown next; picking the shown run again stops the
 * fade. Reduced motion swaps at once.
 */
export function useRunSwap(runId: string | null, pane: RefObject<HTMLElement>) {
  const reduced = useReducedMotion()
  const [shown, setShown] = useState(runId)
  const target = useRef(runId)
  const fade = useRef<Animation | null>(null)
  if (reduced && shown !== runId) setShown(runId)
  const leaving = shown !== runId

  useLayoutEffect(() => {
    target.current = runId
    if (!leaving) {
      fade.current?.cancel()
      fade.current = null
      return
    }
    if (fade.current) return
    const element = must(pane.current, "run pane")
    const style = getComputedStyle(element)
    const animation = element.animate([{ opacity: style.opacity }, { opacity: 0 }], {
      duration: cssMs(style.getPropertyValue("--t-fast")),
      easing: style.getPropertyValue("--ease-in").trim(),
      fill: "forwards",
    })
    fade.current = animation
    animation.finished.then(
      () => {
        fade.current = null
        setShown(target.current)
      },
      () => undefined, // Cancelled: the shown run was picked again, or the page unmounted.
    )
  }, [leaving, runId, pane])

  useEffect(() => () => fade.current?.cancel(), [])

  return { shown, leaving }
}
