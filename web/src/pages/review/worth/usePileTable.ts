import { useEffect, useState } from "react"

import { errorText } from "@/lib/api/http"
import { fetchWorthTable } from "@/lib/api/review"
import { wait } from "@/lib/review/timing"
import { EMPTY, toggled } from "@/lib/sets"
import type { DecisionRow } from "@/types/review"

import { useReview } from "../hooks/useReview"
import { DECIDED } from "./copy"
import { otherPile, pileCount, type Pile, type PileMove } from "./piles"
import { saveWorth } from "./save"
import { useLeftScreen } from "./useLeftScreen"
import type { PileMoves } from "./usePileMoves"

/** A decided pile as far as it has been read. */
interface PileRows {
  /** The pages read so far, in the server's name order, less the rows flipped away. */
  rows: readonly DecisionRow[]
  /** The pile's size as the server last counted it. */
  total: number
}

/**
 * One decided pile's table (reconcile_review.js `appendDecisionPage`, `decideDecisionRow`).
 *
 *   mount   read the first page
 *   more    read the page after the rows held and append it
 *   flip    fade the row, count the move, POST /worth; the answer removes the row and
 *           corrects the counts, a refusal brings the row and its counts back
 *
 * A flipped row has left the pile on the server too, so the next page starts at the number
 * of rows still held, and what is left is the pile's size less those rows. A stage that
 * left the screen does nothing more (useLeftScreen).
 */
export function usePileTable(pile: Pile, moves: PileMoves) {
  const review = useReview()
  const [table, setTable] = useState<PileRows | null>(null)
  /** Why the first page could not be read. */
  const [failure, setFailure] = useState<string | null>(null)
  /** The next page is on its way: "Show more" waits. */
  const [reading, setReading] = useState(false)
  /** Parents whose flip is saving: their rows are fading and take no clicks. */
  const [leaving, setLeaving] = useState(EMPTY)
  const left = useLeftScreen()

  useEffect(() => {
    const gone = new AbortController()
    fetchWorthTable(pile, 0, gone.signal).then(setTable, (error: unknown) => {
      if (!gone.signal.aborted) setFailure(errorText(error))
    })
    return () => gone.abort()
  }, [pile])

  async function more(held: number) {
    setReading(true)
    try {
      const page = await fetchWorthTable(pile, held)
      setTable((before) => before && { rows: [...before.rows, ...page.rows], total: page.total })
    } catch (error) {
      if (!left()) review.toastError(errorText(error))
    }
    setReading(false)
  }

  async function flip(row: DecisionRow) {
    const { parent_id: parent, worth_key: key, slug } = row.person
    const to = otherPile(pile)
    const move: PileMove = { from: pile, to }
    setLeaving((parents) => toggled(parents, parent, true))
    moves.begin(move)
    const fade = wait(review.fadeMs)
    const saved = await saveWorth({ pub: key, worth: to, parent_slug: slug })
    // A refusal brings the row back at once; a saved row finishes its fade first.
    if (saved.ok) await fade
    if (left()) return
    moves.end(move)
    setLeaving((parents) => toggled(parents, parent, false))
    if (!saved.ok) {
      review.toastError(saved.message)
      return
    }
    const { progress } = saved.result
    setTable(
      (before) =>
        before && {
          rows: before.rows.filter((other) => other.person.parent_id !== parent),
          total: pileCount(progress, pile),
        },
    )
    review.applyProgress(progress)
    review.toast(DECIDED[to])
  }

  return { table, failure, reading, leaving, more, flip }
}
