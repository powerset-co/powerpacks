import { useState } from "react"

import { Appear, type ToastMessage } from "@/components/shared"
import { Button } from "@/components/ui/button"
import { errorText } from "@/lib/api/http"

import type { Feedback } from "../hooks/useFeedback"

interface FeedbackStatusProps {
  runId: string
  feedback: Feedback
  onToast: (toast: ToastMessage) => void
}

// results.js feedbackNotice: while the run's queue is stopped, how many records wait on this
// device and the one way on, Retry or a Powerset sign-in. It rises in and drops out.
export function FeedbackStatus({ runId, feedback, onToast }: FeedbackStatusProps) {
  const [signingIn, setSigningIn] = useState(false)
  const waiting = feedback.pending.filter((record) => record.run_id === runId).length
  const needsSignIn = feedback.failure === "needs_auth"

  const act = () => {
    if (!needsSignIn) {
      void feedback.retry()
      return
    }
    setSigningIn(true)
    feedback
      .signIn()
      .catch((error: unknown) => onToast({ message: errorText(error), error: true }))
      .finally(() => setSigningIn(false))
  }

  return (
    <Appear show={feedback.failure !== null && waiting > 0} className="text-xs text-muted-foreground">
      <span data-feedback-waiting>Saved on this device. {waiting.toLocaleString()} waiting to send.</span>
      <Button size="sm" disabled={signingIn} onClick={act}>
        {signingIn ? "Waiting for sign-in…" : needsSignIn ? "Sign in to Powerset" : "Retry"}
      </Button>
    </Appear>
  )
}
