import { useState, type TransitionEvent } from "react"

import { useReducedMotion } from "./useReducedMotion"

/**
 * Keeps an overlay mounted through its exit transition, showing the last value it had.
 * Pass what to show, or null to hide. Render `shown` while `mounted`, set `data-open={open}`
 * and pass `onTransitionEnd`; the `.rise` rule in index.css animates both ways. Under
 * reduced motion no transition runs, so it unmounts at once.
 */
export function usePresence<T>(value: T | null) {
  const reduced = useReducedMotion()
  const show = value !== null
  const [mounted, setMounted] = useState(show)
  const [last, setLast] = useState(value)
  if (show && !mounted) setMounted(true)
  if (!show && mounted && reduced) setMounted(false)
  if (value !== null && value !== last) setLast(value)

  const onTransitionEnd = (event: TransitionEvent<HTMLElement>) => {
    if (!show && event.target === event.currentTarget) setMounted(false)
  }
  return { mounted, open: show, shown: value ?? last, onTransitionEnd }
}
