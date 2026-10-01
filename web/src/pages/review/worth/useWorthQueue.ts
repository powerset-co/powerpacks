import { useCallback, useEffect, useRef, useState } from "react"

import { errorText } from "@/lib/api/http"
import { fetchWorthCard, ReviewError, type WorthCardQuery } from "@/lib/api/review"
import { STAGE_DONE, TOAST } from "@/lib/review/copy"
import { wait } from "@/lib/review/timing"
import { EMPTY, toggled } from "@/lib/sets"

import { useReview } from "../hooks/useReview"
import { DECIDED } from "./copy"
import { panelOf, type CardPanel, type QueuePanel } from "./panel"
import type { Pile, PileMove } from "./piles"
import { saveWorth } from "./save"
import { useLeftScreen } from "./useLeftScreen"
import type { PileMoves } from "./usePileMoves"

/** What the review tab's panel has on screen. */
export interface Shown {
  panel: QueuePanel
  /** Counts the swaps: each one mounts the card's contents anew. */
  swaps: number
  /** The card's contents are fading out: a decision or a picked person is on its way. */
  swapping: boolean
}

interface WorthQueueOptions {
  /** The tabs' optimistic counts. */
  moves: PileMoves
  /** The person is decided: their name leaves the typeahead. */
  forget: (key: string) => void
}

function readPanel(card: WorthCardQuery, signal?: AbortSignal): Promise<QueuePanel> {
  return fetchWorthCard(card, signal).then(panelOf)
}

/** A card read that answers null when the server or the network refuses, so a decision can
 *  wait on it beside its fade. */
function readAhead(card: WorthCardQuery): Promise<QueuePanel | null> {
  return readPanel(card).catch(() => null)
}

type Picked = QueuePanel | "gone" | "failed"

/** One pending person's card by worth key; "gone" when they are no longer pending. */
function readPicked(key: string): Promise<Picked> {
  return readPanel({ pick: key }).catch((error: unknown) =>
    error instanceof ReviewError && error.gone ? "gone" : "failed",
  )
}

/**
 * The review tab's card queue.
 *
 *   mount       read the card for the URL's `index` / `debug`
 *   show        put a card on screen and read the one after it ahead, leaving out the card
 *               shown and every person whose save has not answered
 *   decide      fade the card, count the move, POST /worth in the background, swap in the
 *               card read ahead; the answer corrects the counts (or brings the card back)
 *   pick        the typeahead: read that person's card and swap it in
 *   browse      the debug carousel: read the card at a queue position; nothing is written
 *
 * A stage that left the screen does nothing more (useLeftScreen).
 */
export function useWorthQueue({ moves, forget }: WorthQueueOptions) {
  const review = useReview()
  const [shown, setShown] = useState<Shown | null>(null)
  /** Why the first card could not be read. */
  const [failure, setFailure] = useState<string | null>(null)
  /** Worth keys whose save has not answered yet. */
  const saving = useRef(EMPTY)
  /** The card after the one on screen, read ahead; null once a decision took it. */
  const ahead = useRef<Promise<QueuePanel | null> | null>(null)
  const left = useLeftScreen()

  const show = useCallback((panel: QueuePanel) => {
    setShown((held) => ({ panel, swaps: held ? held.swaps + 1 : 0, swapping: false }))
    ahead.current =
      panel.kind === "card" ? readAhead({ exclude: [...saving.current, panel.person.worth_key] }) : null
  }, [])

  const setSwapping = (swapping: boolean) => setShown((held) => held && { ...held, swapping })

  const { index, debug } = review
  useEffect(() => {
    const gone = new AbortController()
    readPanel({ index, debug }, gone.signal).then(show, (error: unknown) => {
      if (!gone.signal.aborted) setFailure(errorText(error))
    })
    return () => gone.abort()
  }, [index, debug, show])

  async function decide(before: CardPanel, worth: Pile, note: string) {
    const { worth_key: key, slug } = before.person
    const move: PileMove = { from: "review", to: worth }
    setSwapping(true)
    moves.begin(move)
    saving.current = toggled(saving.current, key, true)
    // parent_slug pins the write to the parent this card was drawn from.
    const save = saveWorth({ pub: key, worth, parent_slug: slug, note }).finally(() => {
      saving.current = toggled(saving.current, key, false)
    })
    const upcoming = ahead.current ?? readAhead({ exclude: [key] })
    ahead.current = null
    const [next] = await Promise.all([upcoming, wait(review.fadeMs)])

    if (next === null) {
      // No next card to swap in: wait for the save, then read the screen again.
      const saved = await save
      if (left()) return
      moves.end(move)
      if (!saved.ok) {
        setSwapping(false)
        review.toastError(saved.message)
        return
      }
      review.applyProgress(saved.result.progress)
      review.leaveAndReload(TOAST.saved)
      return
    }

    if (left()) return
    // The last card has no next: its frame holds until the stage check replaces it.
    if (next.kind !== "empty") show(next)
    const saved = await save
    if (left()) return
    moves.end(move)
    if (!saved.ok) {
      show(before)
      review.toastError(saved.message)
      return
    }
    const { progress } = saved.result
    review.applyProgress(progress)
    forget(key)
    review.toast(DECIDED[worth])
    if (progress.worth_pending === 0) review.transition(STAGE_DONE.worth, "enrich")
  }

  async function pick(key: string) {
    if (saving.current.has(key)) {
      forget(key)
      review.toast(TOAST.alreadyDecided)
      return
    }
    const picked = await readPicked(key)
    if (left()) return
    if (picked === "gone") {
      forget(key)
      review.toast(TOAST.alreadyDecided)
      return
    }
    if (picked === "failed") {
      review.toastError(TOAST.cardFailed)
      return
    }
    setSwapping(true)
    await wait(review.fadeMs)
    if (!left()) show(picked)
  }

  async function browse(position: number) {
    const panel = await readAhead({ index: position, debug: true })
    if (left()) return
    if (panel === null) {
      review.toastError(TOAST.cardFailed)
      return
    }
    show(panel)
  }

  return { shown, failure, decide, pick, browse }
}
