import type { ReactNode } from "react"

import "../styles/enrich.css"

interface EnrichMarkProps {
  part: number
  parts: number
  running: boolean
  // What sits inside the ring: the soft shape unless the page puts its own there.
  center?: ReactNode
}

export function EnrichMark({ part, parts, running, center }: EnrichMarkProps) {
  const ring = { cx: 56, cy: 56, r: 52, pathLength: parts }
  return (
    <span className="enrich-mark" aria-hidden="true">
      <svg className="enrich-ring" viewBox="0 0 112 112">
        <circle {...ring} />
        {part > 0 ? <circle {...ring} className="done" strokeDasharray={`${part} ${parts}`} /> : null}
        {part >= 0 && running ? (
          <circle {...ring} className="now" strokeDasharray={`1 ${parts}`} strokeDashoffset={-part} />
        ) : null}
      </svg>
      {running ? <span className="enrich-orbit" /> : null}
      {center ?? <span className="enrich-shape" />}
    </span>
  )
}
