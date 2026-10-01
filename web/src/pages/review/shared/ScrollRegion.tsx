import type { ReactNode } from "react"

import { SCROLL_DOWN } from "@/lib/review/copy"

import { useScrollCue } from "../hooks/useScrollCue"

interface ScrollRegionProps {
  children: ReactNode
}

// The part of a card that scrolls inside it, with a "scroll down" cue while more sits below.
export function ScrollRegion({ children }: ScrollRegionProps) {
  const { scroller, more, scrollDown } = useScrollCue<HTMLDivElement>()
  return (
    <div className="identity-scroll-shell">
      <div className="identity-scroll" ref={scroller}>
        {children}
      </div>
      <button
        className="scroll-cue"
        type="button"
        aria-label={SCROLL_DOWN}
        hidden={!more}
        onClick={scrollDown}
      >
        <svg viewBox="0 0 24 24" aria-hidden="true">
          <path d="m7 9 5 5 5-5" />
        </svg>
      </button>
    </div>
  )
}
