// What the LinkedIn card on screen is waiting for, and what each wait does to it.

export type CardPhase =
  /** Nothing is out. */
  | "ready"
  /** A decision is saving. */
  | "deciding"
  /** The re-research request is out. */
  | "retargeting"
  /** Re-research is queued and the next card is being read. */
  | "moving"

interface CardState {
  /** The contents are faded out and take no clicks. */
  fading: boolean
  /** Every button on the card is off. A card whose re-research is asked for takes no other
   *  decision: the re-research may save the person's No on it. */
  locked: boolean
}

export const CARD: Readonly<Record<CardPhase, CardState>> = {
  ready: { fading: false, locked: false },
  deciding: { fading: true, locked: true },
  retargeting: { fading: false, locked: true },
  moving: { fading: true, locked: true },
}
