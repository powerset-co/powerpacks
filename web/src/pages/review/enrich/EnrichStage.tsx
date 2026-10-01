import "../styles/enrich.css"

import { useState } from "react"

import { errorText } from "@/lib/api/http"
import { approveEnrichment, completeStage } from "@/lib/api/review"
import { STAGE_DONE, TOAST } from "@/lib/review/copy"
import type { EnrichmentJob } from "@/lib/review/sync"
import type { EnrichmentPanel } from "@/types/review"

import { useReview } from "../hooks/useReview"
import { EmptyPanel } from "../shared/EmptyPanel"
import { CONTINUE, TITLE } from "./copy"
import { RunningPanel } from "./RunningPanel"

export interface EnrichStageProps {
  /** The panel as the screen loaded it. */
  enrichment: EnrichmentPanel
  /** The running enrichment's latest numbers from /api/events; null until one arrives. */
  job: EnrichmentJob | null
}

// The Enrich stage: one panel in one of five states. Approve is the app's one spend gate: it
// posts once and the answer's panel replaces this one in place.
// Continue marks the stage complete and runs the stage check to LinkedIn.
export function EnrichStage({ enrichment, job }: EnrichStageProps) {
  const review = useReview()
  const [panel, setPanel] = useState(enrichment)
  /** A POST is in flight: the button waits, and the status watcher with it. */
  const [busy, setBusy] = useState(false)

  const hold = () => {
    setBusy(true)
    review.setCompleting(true)
  }
  const release = () => {
    review.setCompleting(false)
    setBusy(false)
  }
  const refuse = (error: unknown) => {
    release()
    review.toastError(errorText(error))
  }

  const approve = async () => {
    hold()
    try {
      const { enrichment: answer } = await approveEnrichment()
      setPanel(answer)
      release()
      review.noteServerStage("enrich")
      review.toast(TOAST.approved)
      if (answer.mode !== "running") review.syncStatus()
    } catch (error) {
      refuse(error)
    }
  }

  const finish = async () => {
    hold()
    try {
      await completeStage("enrich")
      review.transition(STAGE_DONE.enrich, "linkedin")
    } catch (error) {
      refuse(error)
    }
  }

  switch (panel.mode) {
    case "running":
      return <RunningPanel panel={panel} job={job} />
    case "approval":
      return (
        <EmptyPanel title={TITLE.approval} className="enrich-state">
          <button
            className="button button-primary button-hero"
            type="button"
            disabled={busy}
            aria-busy={busy ? true : undefined}
            onClick={() => void approve()}
          >
            {panel.approval_label}
          </button>
        </EmptyPanel>
      )
    case "completed":
      return (
        <EmptyPanel title={TITLE.completed} className="enrich-state">
          <button
            className="button button-primary"
            type="button"
            disabled={busy}
            aria-busy={busy ? true : undefined}
            onClick={() => void finish()}
          >
            {CONTINUE}
          </button>
        </EmptyPanel>
      )
    case "failed":
      return (
        <EmptyPanel title={TITLE.failed} className="enrich-state">
          <p>{panel.error}</p>
        </EmptyPanel>
      )
    case "preparing":
      return <EmptyPanel title={TITLE.preparing} className="enrich-state" />
  }
}
