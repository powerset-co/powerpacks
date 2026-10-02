import "../styles/enrich.css"

import { useState } from "react"

import type { EnrichPending } from "@/types/review"

import { EmptyPanel } from "../shared/EmptyPanel"
import { doingNow, MOVES_ON, STARTING, TITLE } from "./copy"
import { type Reading, timeLeft, totalLeft } from "./progress"

/** What the latest status read said is left, and when it was read. */
export interface Waiting {
  pending: EnrichPending
  at: number
}

export interface EnrichStageProps {
  /** The latest status read; null until the first arrives. */
  waiting: Waiting | null
}

interface Shown {
  waiting: Waiting | null
  /** The first reading on this screen: the pace is measured from it. */
  start: Reading | null
  left: string
}

// The Enrich stage: the agent runs the enrichment, and this screen only waits. It says what is
// being done and how long the rest takes, from each status read the page makes; the page moves
// on to LinkedIn when the store does.
export function EnrichStage({ waiting }: EnrichStageProps) {
  const [shown, setShown] = useState<Shown>({ waiting: null, start: null, left: "" })
  if (waiting && waiting !== shown.waiting) {
    const now = { at: waiting.at, left: totalLeft(waiting.pending) }
    const start = shown.start ?? now
    setShown({ waiting, start, left: timeLeft(start, now) })
  }

  return (
    <EmptyPanel
      title={TITLE}
      className="enrich-state"
      above={<span className="enrich-shape" aria-hidden="true" />}
    >
      <p className="enrich-doing">{waiting ? doingNow(waiting.pending) : STARTING}</p>
      <p className="enrich-time-left">{shown.left || MOVES_ON}</p>
    </EmptyPanel>
  )
}
