import { useRef } from "react"

import { must } from "@/lib/must"
import type { ReviewCandidate, ReviewPerson } from "@/types/review"

import { DecisionCard } from "../shared/DecisionCard"
import { PersonCard } from "../shared/PersonCard"
import { ScrollRegion } from "../shared/ScrollRegion"
import { ANSWER, WHY } from "./copy"
import type { Pile } from "./piles"

const NOTE_MAX = 2000

interface WorthCardProps {
  person: ReviewPerson
  candidate: ReviewCandidate | null
  /** A new key mounts the contents anew: the next person, or this one brought back. */
  cardKey: string
  /** The contents are fading out and the buttons are off: a click has been taken. */
  swapping: boolean
  /** Yes or No, with whatever the note box holds. */
  onDecide: (worth: Pile, note: string) => void
}

// templates/worth_card.html.j2: the person (scrolling inside the card), the optional note box,
// and No / Yes.
export function WorthCard({ person, candidate, cardKey, swapping, onDecide }: WorthCardProps) {
  return (
    <DecisionCard cardKey={cardKey} swapping={swapping} className="identity-card worth-card">
      <Contents person={person} candidate={candidate} locked={swapping} onDecide={onDecide} />
    </DecisionCard>
  )
}

interface ContentsProps extends Pick<WorthCardProps, "person" | "candidate" | "onDecide"> {
  locked: boolean
}

function Contents({ person, candidate, locked, onDecide }: ContentsProps) {
  const note = useRef<HTMLTextAreaElement>(null)
  const decide = (worth: Pile) => onDecide(worth, must(note.current, "the note box").value.trim())
  return (
    <>
      <ScrollRegion>
        <PersonCard person={person} candidate={candidate} />
      </ScrollRegion>
      <details className="worth-why">
        <summary>{WHY}</summary>
        <textarea ref={note} rows={2} maxLength={NOTE_MAX} />
      </details>
      <div className="binary-actions">
        <button
          className="button button-outline"
          type="button"
          disabled={locked}
          onClick={() => decide("no")}
        >
          {ANSWER.no}
        </button>
        <button
          className="button button-primary"
          type="button"
          disabled={locked}
          onClick={() => decide("yes")}
        >
          {ANSWER.yes}
        </button>
      </div>
    </>
  )
}
