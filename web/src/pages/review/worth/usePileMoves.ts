import { useCallback, useMemo, useState } from "react"

import { useReview } from "../hooks/useReview"
import { tabCounts, type PileMove, type TabCounts } from "./piles"

export interface PileMoves {
  /** What the tabs show: the page's counts with every move still saving applied. */
  counts: TabCounts
  /** A decision was clicked: its move counts at once. */
  begin: (move: PileMove) => void
  /** Its save answered or failed: the move stops counting. */
  end: (move: PileMove) => void
}

/** The optimistic tab counts. A click's move counts until its own save ends, so a failed save
 *  takes back exactly its own click. */
export function usePileMoves(): PileMoves {
  const { progress } = useReview()
  const [moves, setMoves] = useState<readonly PileMove[]>([])
  const begin = useCallback((move: PileMove) => setMoves((saving) => [...saving, move]), [])
  const end = useCallback(
    (move: PileMove) => setMoves((saving) => saving.filter((other) => other !== move)),
    [],
  )
  const counts = useMemo(() => tabCounts(progress, moves), [progress, moves])
  return useMemo(() => ({ counts, begin, end }), [counts, begin, end])
}
