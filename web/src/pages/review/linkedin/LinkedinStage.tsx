import "../styles/linkedin.css"

import { LOAD_FAILED } from "@/lib/review/copy"
import { must } from "@/lib/must"

import { CarouselNav } from "../shared/CarouselNav"
import { EmptyPanel } from "../shared/EmptyPanel"
import { SynthesisPending } from "../shared/SynthesisPending"
import { FinishedPanel } from "./FinishedPanel"
import { LinkedinCard } from "./LinkedinCard"
import { useLinkedinQueue } from "./useLinkedinQueue"

// The LinkedIn stage: the queue's card in its swap panel, or what stands in for it once the
// queue is empty. It takes no props: its cards come from its own reads, and the screen's
// settings from `useReview()`.
export function LinkedinStage() {
  const { shown, failure, decide, retarget, browse } = useLinkedinQueue()
  return (
    <div className="linkedin-stage">
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
  const { payload, phase, opening } = shown
  const { card, queue } = payload
  if (card) {
    return (
      <>
        {queue ? <CarouselNav queue={queue} onIndex={onBrowse} /> : null}
        {/* A carousel card is a frame of its own, replaced whole: browsing (and the first
            decision, which leaves the carousel) never plays the swap. */}
        <LinkedinCard
          key={queue ? queue.index : "queue"}
          card={card}
          phase={phase}
          onDecide={onDecide}
          onRetarget={onRetarget}
        />
      </>
    )
  }

  const finished = must(payload.finished, "the finished state")
  if (finished.synthesize_pending) return <SynthesisPending />
  return <FinishedPanel finished={finished} pressItself={opening && finished.auto_continue} />
}
