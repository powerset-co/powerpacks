import { useState, type TransitionEvent } from "react"

import { useReducedMotion } from "./useReducedMotion"

/** The element's own exit finished: its opacity, the one property every exit fades. */
export function ended(event: TransitionEvent<HTMLElement>): boolean {
  return event.target === event.currentTarget && event.propertyName === "opacity"
}

/**
 * Keeps an overlay mounted through its exit transition, showing the last value it had.
 * Pass what to show, or null to hide. Render `shown` while `mounted`, set `data-open={open}`
 * and pass `onTransitionEnd`; the `.rise` rule in index.css animates both ways. It unmounts
 * when the exit's opacity ends: another property (a press's translate) can end first. Under
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
    if (!show && ended(event)) setMounted(false)
  }
  return { mounted, open: show, shown: value ?? last, onTransitionEnd }
}
