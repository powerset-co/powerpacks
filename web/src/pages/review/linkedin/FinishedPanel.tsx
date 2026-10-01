import { useEffect, useRef, useState } from "react"

import { errorText } from "@/lib/api/http"
import { completeStage } from "@/lib/api/review"
import { STAGE_DONE } from "@/lib/review/copy"
import type { LinkedinFinished } from "@/types/review"

import { useReview } from "../hooks/useReview"
import { EmptyPanel } from "../shared/EmptyPanel"
import { GoBack } from "../shared/GoBack"
import { decisionsSaved, FINISHED, researchRunning } from "./copy"

interface FinishedPanelProps {
  finished: LinkedinFinished
  /** Finish presses itself once: the screen opened on this state and the server asked for it. */
  pressItself: boolean
}

// The queue has nothing left to show. With every person decided it hands back to Codex;
// otherwise Finish marks the stage complete and the screen loads again.
export function FinishedPanel({ finished, pressItself }: FinishedPanelProps) {
  const { toastError, transition, setCompleting } = useReview()
  const [finishing, setFinishing] = useState(false)
  const button = useRef<HTMLButtonElement>(null)
  const pressed = useRef(false)

  // Finish presses itself: one press, however often the effect runs.
  useEffect(() => {
    if (!pressItself || pressed.current) return
    pressed.current = true
    button.current?.click()
  }, [pressItself])

  const finish = async () => {
    setFinishing(true)
    setCompleting(true)
    try {
      await completeStage("linkedin")
    } catch (error) {
      setCompleting(false)
      setFinishing(false)
      toastError(errorText(error))
      return
    }
    transition(STAGE_DONE.linkedin, "linkedin")
  }

  return (
    <EmptyPanel title={FINISHED.title}>
      <p>{decisionsSaved(finished.linkedin_done)}</p>
      {finished.retargets_in_flight ? <p>{researchRunning(finished.retargets_in_flight)}</p> : null}
      {finished.linkedin_complete ? (
        <GoBack />
      ) : (
        <button
          ref={button}
          className="button button-primary"
          type="button"
          disabled={finishing}
          aria-busy={finishing || undefined}
          onClick={() => void finish()}
        >
          {FINISHED.finish}
        </button>
      )}
    </EmptyPanel>
  )
}
