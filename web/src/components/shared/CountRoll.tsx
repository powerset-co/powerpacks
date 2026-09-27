import { useEffect, useRef, useState } from "react"

import { useReducedMotion } from "@/hooks/useReducedMotion"
import { cssMs } from "@/lib/motion"

interface CountRollProps {
  value: number
  className?: string
}

// A count that rolls to its new value over --t-slow (cubic ease-out) instead of jumping.
export function CountRoll({ value, className }: CountRollProps) {
  const reduced = useReducedMotion()
  const [shown, setShown] = useState(value)
  const shownRef = useRef(value)

  useEffect(() => {
    const from = shownRef.current
    const show = (next: number) => {
      shownRef.current = next
      setShown(next)
    }
    if (from === value || reduced || document.visibilityState === "hidden") {
      show(value)
      return
    }
    const duration = cssMs(getComputedStyle(document.documentElement).getPropertyValue("--t-slow"))
    const startedAt = performance.now()
    let frame = 0
    const step = (now: number) => {
      const t = Math.min(1, (now - startedAt) / duration)
      show(Math.round(from + (value - from) * (1 - (1 - t) ** 3)))
      if (t < 1) frame = window.requestAnimationFrame(step)
    }
    frame = window.requestAnimationFrame(step)
    return () => window.cancelAnimationFrame(frame)
  }, [value, reduced])

  return <span className={className}>{shown.toLocaleString()}</span>
}
