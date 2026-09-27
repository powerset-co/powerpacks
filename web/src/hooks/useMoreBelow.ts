import { useEffect, useState, type RefObject } from "react"

/**
 * True while `box` can still scroll down: content sits below its fold. Follows scrolling
 * and size changes, so a cue can fade out at the end.
 */
export function useMoreBelow(box: RefObject<HTMLElement>): boolean {
  const [more, setMore] = useState(false)
  useEffect(() => {
    const element = box.current
    if (!element) return
    const measure = () => {
      setMore(element.scrollTop + element.clientHeight < element.scrollHeight - 1)
    }
    measure()
    element.addEventListener("scroll", measure, { passive: true })
    const observer = new ResizeObserver(measure)
    observer.observe(element)
    return () => {
      element.removeEventListener("scroll", measure)
      observer.disconnect()
    }
  }, [box])
  return more
}
