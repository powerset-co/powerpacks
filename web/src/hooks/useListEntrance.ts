import { useEffect, useLayoutEffect, useRef, type MutableRefObject } from "react"

import { motionTokens } from "@/lib/motion"

import { useReducedMotion } from "./useReducedMotion"

const STAGGER_MS = 8
const MAX_DELAY_MS = 96

// A cancelled entrance leaves its row where the entrance would have ended.
function cancel(running: MutableRefObject<Animation[]>): void {
  running.current.forEach((animation) => animation.cancel())
  running.current = []
}

/**
 * Rows fade and rise in after the list changes (a view, a filter, a run): every mounted
 * `selector` row, 8ms apart, none more than 96ms behind the first. Scrolling, selection and
 * writes never replay it. `identity` changes identity exactly when the list does; the rows of
 * the new list are in the DOM in the same commit (VirtualRows mounts on first render). Timing
 * is index.css's --t-med and --ease-out. A new list, unmounting, or reduced motion switching on
 * stops the entrance.
 */
export function useListEntrance(
  viewport: () => HTMLElement | null,
  identity: object,
  count: number,
  selector: string,
) {
  const reduced = useReducedMotion()
  const played = useRef<object | null>(null)
  const running = useRef<Animation[]>([])

  useEffect(() => {
    if (reduced) cancel(running)
  }, [reduced])
  useEffect(() => () => cancel(running), [])

  useLayoutEffect(() => {
    if (played.current === identity) return
    played.current = identity
    cancel(running)
    const element = viewport()
    if (count === 0 || reduced || !element) return
    const { duration, easing } = motionTokens(element)
    running.current = [...element.querySelectorAll<HTMLElement>(selector)].map((row, position) =>
      row.animate(
        [
          { opacity: 0, translate: "0 6px" },
          { opacity: 1, translate: "0 0" },
        ],
        {
          duration,
          delay: Math.min(position * STAGGER_MS, MAX_DELAY_MS),
          easing,
          fill: "backwards",
        },
      ),
    )
  })
}
