import "../styles/linkedin.css"

import { LOAD_FAILED } from "@/lib/review/copy"
import { must } from "@/lib/must"

import { CarouselNav } from "../shared/CarouselNav"
import { EmptyPanel } from "../shared/EmptyPanel"
import { SynthesisPending } from "../shared/SynthesisPending"
import { leftToCheck } from "./copy"
import { FinishedPanel } from "./FinishedPanel"
import { LinkedinCard } from "./LinkedinCard"
import { useLinkedinQueue } from "./useLinkedinQueue"

// The LinkedIn stage: the count of people left, over the queue's card in its swap panel, or
// what stands in for the card once the queue is empty. It takes no props: its cards come from its own reads, and the screen's
// settings from `useReview()`.
export function LinkedinStage() {
  const { shown, failure, decide, retarget, browse } = useLinkedinQueue()
  return (
    <div className="linkedin-stage">
      {shown?.payload.card ? <p className="queue-left">{leftToCheck(shown.payload.pending)}</p> : null}
      <div className="linkedin-panel" aria-busy={!shown && !failure ? true : undefined}>
        {failure ? (
          <EmptyPanel title={LOAD_FAILED}>
            <p>{failure}</p>
          </EmptyPanel>
        ) : shown ? (
          <Panel shown={shown} onDecide={decide} onRetarget={retarget} onBrowse={browse} />
        ) : null}
      </div>
    </div>
  )
}

type Queue = ReturnType<typeof useLinkedinQueue>

interface PanelProps {
  shown: NonNullable<Queue["shown"]>
  onDecide: Queue["decide"]
  onRetarget: Queue["retarget"]
  onBrowse: Queue["browse"]
}

function Panel({ shown, onDecide, onRetarget, onBrowse }: PanelProps) {
  const { payload, phase } = shown
  const { card, queue } = payload
  if (card) {
    return (
      <>
        {/* Browsing could reach a card whose decision is out: the arrows wait for this one. */}
        {queue && phase === "ready" ? <CarouselNav queue={queue} onIndex={onBrowse} /> : null}
        {/* One frame for the queue and the carousel alike: browsing swaps the contents the way
            a decision does, so the carousel shows the card exactly as the review will. */}
        <LinkedinCard card={card} phase={phase} onDecide={onDecide} onRetarget={onRetarget} />
      </>
    )
  }

  const finished = must(payload.finished, "the finished state")
  if (finished.synthesize_pending) return <SynthesisPending />
  return <FinishedPanel />
}
