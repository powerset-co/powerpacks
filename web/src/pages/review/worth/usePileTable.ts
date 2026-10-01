import { useEffect, useRef, useState } from "react"

import { errorText } from "@/lib/api/http"
import { fetchWorthTable } from "@/lib/api/review"
import { wait } from "@/lib/review/timing"
import { EMPTY, toggled } from "@/lib/sets"
import type { DecisionRow } from "@/types/review"

import { useReview } from "../hooks/useReview"
import { DECIDED } from "./copy"
import { otherPile, type Pile, type PileMove } from "./piles"
import { saveWorth } from "./save"
import { useLeftScreen } from "./useLeftScreen"
import type { PileMoves } from "./usePileMoves"

/** A decided pile as far as it has been read. */
export interface PileRows {
  /** The pages read so far, in the server's name order, less the rows flipped away. */
  rows: readonly DecisionRow[]
  /** How many rows the pile holds; the reading stops once this many are held. */
  total: number
}

/** A page read from the end of the rows held. An empty page means the pile ends here. */
function appended({ rows, total }: PileRows, page: readonly DecisionRow[]): PileRows {
  return { rows: [...rows, ...page], total: page.length ? total : rows.length }
}

/** The pile without a row that was flipped away: one row fewer, held and in all. */
function without({ rows, total }: PileRows, slug: string): PileRows {
  return { rows: rows.filter((row) => row.person.slug !== slug), total: total - 1 }
}

/**
 * One decided pile's rows (reconcile_review.js `appendDecisionPage`, `decideDecisionRow`).
 *
 *   mount   read the first page and the pile's size
 *   more    read the page after the rows held and append it; one read at a time, and none
 *           once every row is held
 *   flip    fade the row, count the move, POST /worth; the answer removes the row and
 *           corrects the counts, a refusal brings the row and its counts back
 *
 * A flipped row has left the pile on the server too, so the next page starts at the number
 * of rows still held. A stage that left the screen does nothing more (useLeftScreen).
 */
export function usePileTable(pile: Pile, moves: PileMoves) {
  const review = useReview()
  const [table, setTable] = useState<PileRows | null>(null)
  /** Why the first page could not be read. */
  const [failure, setFailure] = useState<string | null>(null)
  /** The next page is on its way: the list says so. */
  const [reading, setReading] = useState(false)
  /** The same, for the callers that ask again before the list has drawn it. */
  const busy = useRef(false)
  /** Slugs whose flip is saving: their rows are fading and take no clicks. */
  const [leaving, setLeaving] = useState(EMPTY)
  const left = useLeftScreen()

  useEffect(() => {
    const gone = new AbortController()
    fetchWorthTable(pile, 0, gone.signal).then(setTable, (error: unknown) => {
      if (!gone.signal.aborted) setFailure(errorText(error))
    })
    return () => gone.abort()
  }, [pile])

  async function more() {
    if (busy.current || !table || table.rows.length >= table.total) return
    busy.current = true
    setReading(true)
    try {
      const page = await fetchWorthTable(pile, table.rows.length)
      setTable((before) => before && appended(before, page.rows))
    } catch (error) {
      if (!left()) review.toastError(errorText(error))
    }
    busy.current = false
    setReading(false)
  }

  async function flip(row: DecisionRow) {
    const { worth_key: key, slug } = row.person
    const to = otherPile(pile)
    const move: PileMove = { from: pile, to }
    setLeaving((slugs) => toggled(slugs, slug, true))
    moves.begin(move)
    const fade = wait(review.fadeMs)
    const saved = await saveWorth({ pub: key, worth: to, parent_slug: slug })
    // A refusal brings the row back at once; a saved row finishes its fade first.
    if (saved.ok) await fade
    if (left()) return
    moves.end(move)
    setLeaving((slugs) => toggled(slugs, slug, false))
    if (!saved.ok) {
      review.toastError(saved.message)
      return
    }
    setTable((before) => before && without(before, slug))
    review.applyProgress(saved.result.progress)
    review.toast(DECIDED[to])
  }

  return { table, failure, reading, leaving, more, flip }
}
