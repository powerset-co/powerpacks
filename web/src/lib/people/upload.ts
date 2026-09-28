// What the share dialog shows: one phase from the server's status and the browser's own start.

import type { UploadAction, UploadState, UploadStatus } from "@/lib/api/upload"

export type CalmPhase = "idle" | "checking" | "ready" | "uploading" | "completed"
export type FailedPhase = "check-failed" | "upload-failed" | "interrupted" | "refused"
export type UploadPhase = CalmPhase | FailedPhase

interface CalmView {
  phase: CalmPhase
}
interface FailedView {
  phase: FailedPhase
  error: string | null
}
export type UploadView = CalmView | FailedView

/** A start the server refused or never answered: its sentence, until the next start or run. */
export interface Refusal {
  action: UploadAction
  error: string
}

export function isActive(state: UploadState): boolean {
  return state === "checking" || state === "uploading"
}

/** First rule wins: a pending start, then a refused one, then the server's status. */
export function uploadView(
  status: UploadStatus | undefined,
  starting: UploadAction | null,
  refused: Refusal | null,
): UploadView {
  if (starting) return { phase: starting === "check" ? "checking" : "uploading" }
  if (refused) return { phase: refused.action === "check" ? "check-failed" : "refused", error: refused.error }
  if (!status) return { phase: "idle" }
  const upload = status.failed_action === "upload"
  if (status.status === "failed")
    return { phase: upload ? "upload-failed" : "check-failed", error: status.error }
  if (status.status === "interrupted")
    return { phase: upload ? "interrupted" : "check-failed", error: status.error }
  return { phase: status.status }
}

/** Confirming after a failed or interrupted upload picks it up again. */
export function resumes(status: UploadStatus | undefined): boolean {
  const last = status?.last_upload
  return Boolean(last && last.status !== "completed")
}
