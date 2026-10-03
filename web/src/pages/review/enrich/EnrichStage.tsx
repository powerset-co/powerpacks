import "../styles/enrich.css"

import type { ReviewStatus } from "@/types/review"

import { EmptyPanel } from "../shared/EmptyPanel"
import { EnrichMark } from "../shared/EnrichMark"
import {
  doingNow,
  ENRICHED,
  OPENING_REVIEW,
  partOf,
  PARTS,
  READY_TITLE,
  STARTING,
  timeLeft,
  TITLE,
} from "./copy"

export interface EnrichStageProps {
  /** The latest status read; null until the first arrives. */
  status: ReviewStatus | null
  /** The run completed while this screen watched: the review is about to open. */
  done: boolean
}

// The Enrich stage: the agent runs the enrichment, and this screen only waits. Everything it
// says comes from the latest status read: the step the run is on, what is left, and about how
// long. When the run completes it says so, its ring full, and the page opens the review. The
// time left keeps its line while empty, so nothing jumps when it arrives.
export function EnrichStage({ status, done }: EnrichStageProps) {
  const part = done ? PARTS : status ? partOf(status.step) : -1
  const doing = status ? doingNow(status.step, status.pending) : STARTING
  const left = status ? timeLeft(status.minutes_left) : ""

  return (
    <EmptyPanel
      title={done ? READY_TITLE : TITLE}
      className="enrich-state"
      above={<EnrichMark part={part} parts={PARTS} running={!done} />}
    >
      <p className="enrich-doing">
        {done ? ENRICHED : doing}
        {done ? null : (
          <span className="enrich-dots" aria-hidden="true">
            <i>.</i>
            <i>.</i>
            <i>.</i>
          </span>
        )}
      </p>
      <p className="enrich-time-left">{done ? OPENING_REVIEW : left}</p>
    </EmptyPanel>
  )
}
