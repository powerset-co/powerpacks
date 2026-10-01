// What the review tab's panel draws for one card read (server.py `worth_body`): the next
// person's card, the synthesis handoff, or nothing.

import type { QueuePosition, ReviewCandidate, ReviewPerson, WorthCardPayload } from "@/types/review"

/** One undecided person, and where the card sits in the queue under `debug=1`. */
export interface CardPanel {
  kind: "card"
  person: ReviewPerson
  candidate: ReviewCandidate | null
  queue: QueuePosition | null
}

export type QueuePanel =
  | CardPanel
  /** Nobody to show because synthesis has not run. */
  | { kind: "synthesis" }
  /** Nobody to show: every pending person is decided or saving. */
  | { kind: "empty" }

export function panelOf({ card, synthesize_pending, queue }: WorthCardPayload): QueuePanel {
  if (card) return { kind: "card", person: card.person, candidate: card.candidate, queue }
  return synthesize_pending ? { kind: "synthesis" } : { kind: "empty" }
}
