import { useEffect, useRef } from "react"

/** Text entry, where a key types instead of acting. A checkbox is not one: Escape on a focused
 *  checkbox still reaches the page. */
export function isTyping(target: Element): boolean {
  return target.matches("input:not([type=checkbox]), select, textarea")
}

/**
 * The page's document keydown listener, added once; `handler` is the latest render's, so it
 * reads current state. It gets the event's element target (the body when there is none).
 */
export function useKeys(handler: (event: KeyboardEvent, target: Element) => void): void {
  const latest = useRef(handler)
  useEffect(() => {
    latest.current = handler
  })

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      latest.current(event, event.target instanceof Element ? event.target : document.body)
    }
    document.addEventListener("keydown", onKey)
    return () => document.removeEventListener("keydown", onKey)
  }, [])
}
