import { LOAD_FAILED } from "@/lib/review/copy"

import { CarouselNav } from "../shared/CarouselNav"
import { EmptyPanel } from "../shared/EmptyPanel"
import { SynthesisPending } from "../shared/SynthesisPending"
import type { PileMoves } from "./usePileMoves"
import { useWorthNames } from "./useWorthNames"
import { useWorthQueue } from "./useWorthQueue"
import { WorthCard } from "./WorthCard"
import { WorthSearch } from "./WorthSearch"

interface WorthQueueProps {
  /** The tabs' optimistic counts. */
  moves: PileMoves
}

// The review tab (server.py `full_page`, `worth_body`): the typeahead over the pending people
// (outside the panel, so a card swap never touches it), then the panel with one person's card.
export function WorthQueue({ moves }: WorthQueueProps) {
  const names = useWorthNames()
  const queue = useWorthQueue({ moves, forget: names.forget })
  return (
    <>
      {names.searchable ? <WorthSearch names={names.pending} onPick={(key) => void queue.pick(key)} /> : null}
      <div className="worth-panel">
        <Panel queue={queue} />
      </div>
    </>
  )
}

function Panel({ queue }: { queue: ReturnType<typeof useWorthQueue> }) {
  const { shown, failure } = queue
  if (failure !== null) {
    return (
      <EmptyPanel title={LOAD_FAILED}>
        <p>{failure}</p>
      </EmptyPanel>
    )
  }
  if (!shown) return null

  const { panel, swaps, swapping } = shown
  if (panel.kind === "synthesis") return <SynthesisPending />
  if (panel.kind === "empty") return null

  const card = (
    <WorthCard
      person={panel.person}
      candidate={panel.candidate}
      cardKey={`${panel.person.worth_key}:${swaps}`}
      swapping={swapping}
      onDecide={(worth, note) => void queue.decide(panel, worth, note)}
    />
  )
  if (!panel.queue) return card
  // `debug=1`: Previous and Next around the card.
  return (
    <div className="carousel-shell">
      <CarouselNav queue={panel.queue} onIndex={(position) => void queue.browse(position)} />
      {card}
    </div>
  )
}
