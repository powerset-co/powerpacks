// What the LinkedIn card on screen is waiting for, and what each wait does to it
// (reconcile_review.js `decideLinkedinCard` and the retarget submit handler).

export type CardPhase =
  /** Nothing is out. */
  | "ready"
  /** A decision is saving. */
  | "deciding"
  /** The re-research request is out. */
  | "retargeting"
  /** Re-research is queued and the next card is being read. */
  | "moving"
  /** Re-research is queued but the next card could not be read: this card stays. */
  | "queued"

interface CardState {
  /** The contents are faded out and take no clicks. */
  fading: boolean
  /** Every button on the card is off. */
  locked: boolean
  /** Retarget is off: the paid request is sent once per card. */
  retargetOff: boolean
  /** The guidance box says the re-research is queued. */
  queuedNote: boolean
}

export const CARD: Readonly<Record<CardPhase, CardState>> = {
  ready: { fading: false, locked: false, retargetOff: false, queuedNote: false },
  deciding: { fading: true, locked: true, retargetOff: true, queuedNote: false },
  retargeting: { fading: false, locked: false, retargetOff: true, queuedNote: false },
  moving: { fading: true, locked: false, retargetOff: true, queuedNote: false },
  queued: { fading: false, locked: false, retargetOff: true, queuedNote: true },
}
