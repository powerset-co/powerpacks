import { useEffect, useState } from "react"

import { errorText } from "@/lib/api/http"
import {
  fetchLinkedinCard,
  postDecide,
  postRetarget,
  type DecideRequest,
  type RetargetRequest,
} from "@/lib/api/review"
import { TOAST } from "@/lib/review/copy"
import { wait } from "@/lib/review/timing"
import type { DecideResult, LinkedinCardPayload } from "@/types/review"

import { useReview } from "../hooks/useReview"
import type { CardPhase } from "./phase"

/** What the LinkedIn panel shows. */
export interface Shown {
  payload: LinkedinCardPayload
  phase: CardPhase
}

/**
 * The LinkedIn queue, one card at a time. The stage reads its first card on mount. A
 * decision's answer carries the next card, read after the write, so a decided person never
 * comes back; a queued re-research reads the next card itself, leaving that person out. The
 * re-research settles that person in the background: they never come back as a card.
 */
export function useLinkedinQueue() {
  const { debug, index, fadeMs, toast, toastError, applyProgress, leaveAndReload } = useReview()
  const [shown, setShown] = useState<Shown | null>(null)
  /** Why the first read failed; "" while it has not. */
  const [failure, setFailure] = useState("")

  useEffect(() => {
    const read = new AbortController()
    fetchLinkedinCard({ debug, index }, read.signal).then(
      (payload) => setShown({ payload, phase: "ready" }),
      (error: unknown) => {
        if (!read.signal.aborted) setFailure(errorText(error))
      },
    )
    return () => read.abort()
  }, [debug, index])

  const setPhase = (phase: CardPhase) => setShown((current) => current && { ...current, phase })
  const follow = (payload: LinkedinCardPayload) => setShown({ payload, phase: "ready" })

  const decide = async (request: DecideRequest, message: string) => {
    setPhase("deciding")
    let response: DecideResult
    try {
      ;[response] = await Promise.all([postDecide(request), wait(fadeMs)])
    } catch (error) {
      // The save failed: the undecided card comes back.
      setPhase("ready")
      toastError(errorText(error))
      return
    }

    const pending = response.next.pending
    applyProgress({ linkedin_pending: pending })
    // The answer carries the next card, or the finished state after the last decision: either
    // way it goes straight on screen (no stage-check pause, no reload).
    follow(response.next)
    if (pending > 0) toast(message)
  }

  const retarget = async (request: RetargetRequest) => {
    setPhase("retargeting")
    try {
      await postRetarget(request)
    } catch (error) {
      setPhase("ready")
      toastError(errorText(error))
      return
    }

    toast(TOAST.queued)
    setPhase("deciding")
    const [next] = await Promise.all([
      fetchLinkedinCard({ exclude: [request.parent_slug] }).catch(() => null),
      wait(fadeMs),
    ])
    if (next) {
      follow(next)
      return
    }

    // The next card could not be read: the screen reads itself again, without the person now
    // in re-research.
    setPhase("retargeting")
    leaveAndReload(TOAST.queued)
  }

  const browse = async (position: number) => {
    setPhase("deciding")
    try {
      const [next] = await Promise.all([fetchLinkedinCard({ debug: true, index: position }), wait(fadeMs)])
      follow(next)
    } catch {
      setPhase("ready")
      toastError(TOAST.cardFailed)
    }
  }

  return {
    shown,
    failure,
    /** Saves a decision; `message` is the toast when another card follows. */
    decide: (request: DecideRequest, message: string) => void decide(request, message),
    /** Queues the PAID re-research, then moves on to the next card. */
    retarget: (request: RetargetRequest) => void retarget(request),
    /** The debug carousel: reads the card at a queue position. Nothing is written. */
    browse: (position: number) => void browse(position),
  }
}
