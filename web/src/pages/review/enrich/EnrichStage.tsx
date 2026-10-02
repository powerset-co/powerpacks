import "../styles/enrich.css"

import { useState } from "react"

import { EmptyPanel } from "../shared/EmptyPanel"
import { doingNow, STARTING, TITLE } from "./copy"
import { type Reading, timeLeft } from "./progress"

/** What the latest status read said is left, and when it was read. */
export type Waiting = Reading

export interface EnrichStageProps {
  /** The latest status read; null until the first arrives. */
  waiting: Waiting | null
}

interface Shown {
  waiting: Waiting | null
  /** The first reading on this screen: the lookups' pace is measured from it. */
  start: Waiting | null
  left: string
}

// The Enrich stage: the agent runs the enrichment, and this screen only waits. It says what is
// being done and how long the rest takes, from each status read the page makes; the page moves
// on to LinkedIn when the store does. The time left keeps its line while empty, so nothing jumps
// when it arrives.
export function EnrichStage({ waiting }: EnrichStageProps) {
  const [shown, setShown] = useState<Shown>({ waiting: null, start: null, left: "" })
  if (waiting && waiting !== shown.waiting) {
    const start = shown.start ?? waiting
    setShown({ waiting, start, left: timeLeft(start, waiting) })
  }

  return (
    <EmptyPanel
      title={TITLE}
      className="enrich-state"
      above={<span className="enrich-shape" aria-hidden="true" />}
    >
      <p className="enrich-doing">
        {waiting ? doingNow(waiting.pending) : STARTING}
        <span className="enrich-dots" aria-hidden="true">
          <i>.</i>
          <i>.</i>
          <i>.</i>
        </span>
      </p>
      <p className="enrich-time-left">{shown.left}</p>
    </EmptyPanel>
  )
}
