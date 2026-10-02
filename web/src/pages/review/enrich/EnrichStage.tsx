import "../styles/enrich.css"

import type { ReviewStatus } from "@/types/review"

import { EmptyPanel } from "../shared/EmptyPanel"
import { doingNow, partOf, PARTS, STARTING, timeLeft, TITLE } from "./copy"

export interface EnrichStageProps {
  /** The latest status read; null until the first arrives. */
  status: ReviewStatus | null
}

// The Enrich stage: the agent runs the enrichment, and this screen only waits. Everything it
// says comes from the latest status read: the step the run is on, what is left, and about how
// long. The page moves on to LinkedIn when the run completes. The time left keeps its line
// while empty, so nothing jumps when it arrives.
export function EnrichStage({ status }: EnrichStageProps) {
  const part = status ? partOf(status.step) : -1
  const ring = { cx: 56, cy: 56, r: 52, pathLength: PARTS }

  return (
    <EmptyPanel
      title={TITLE}
      className="enrich-state"
      above={
        <span className="enrich-mark" aria-hidden="true">
          <svg className="enrich-ring" viewBox="0 0 112 112">
            <circle {...ring} />
            {part > 0 ? <circle {...ring} className="done" strokeDasharray={`${part} ${PARTS}`} /> : null}
            {part >= 0 ? (
              <circle {...ring} className="now" strokeDasharray={`1 ${PARTS}`} strokeDashoffset={-part} />
            ) : null}
          </svg>
          <span className="enrich-orbit" />
          <span className="enrich-shape" />
        </span>
      }
    >
      <p className="enrich-doing">
        {status ? doingNow(status.step, status.pending) : STARTING}
        <span className="enrich-dots" aria-hidden="true">
          <i>.</i>
          <i>.</i>
          <i>.</i>
        </span>
      </p>
      <p className="enrich-time-left">{status ? timeLeft(status.minutes_left) : ""}</p>
    </EmptyPanel>
  )
}
