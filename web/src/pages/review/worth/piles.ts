// The worth screen's piles: the pending queue (Review) and the two decided piles (Yes, No),
// and the counts their tabs show while decisions save (reconcile_review.js `bumpTabCount`).

import type { DecisionProgress, WorthTab } from "@/types/review"

/** A decided pile: where a Yes or a No puts a person. */
export type Pile = Exclude<WorthTab, "review">

/** The pile a decided row flips to. */
export function otherPile(pile: Pile): Pile {
  return pile === "yes" ? "no" : "yes"
}

/** One person leaving a pile for another, counted on the tabs from the click until its save
 *  answers (the response's counts) or fails (the click's counts). */
export interface PileMove {
  from: WorthTab
  to: Pile
}

export type TabCounts = Readonly<Record<WorthTab, number>>

/** The tabs' counts: the server's, with every move still saving applied. Never below 0. */
export function tabCounts(progress: DecisionProgress, moves: readonly PileMove[]): TabCounts {
  const counts = { review: progress.worth_pending, yes: progress.worth_yes, no: progress.worth_no }
  for (const { from, to } of moves) {
    counts[from] -= 1
    counts[to] += 1
  }
  return { review: Math.max(0, counts.review), yes: Math.max(0, counts.yes), no: Math.max(0, counts.no) }
}
