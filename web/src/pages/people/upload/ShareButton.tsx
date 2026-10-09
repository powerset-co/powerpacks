import { Button } from "@/components/ui/button"
import { DialogTrigger } from "@/components/ui/dialog"
import type { UploadStatus } from "@/lib/api/upload"
import { UPLOAD } from "@/lib/people/copy"

/** Where the share stands, from the check of the current share list. */
type ShareState = "running" | "never" | "current" | "pending"

function shareState(status: UploadStatus | undefined, busy: boolean): ShareState {
  // A check or upload is running, or one is about to start on its own.
  if (busy || !status || status.status === "idle") return "running"
  if (status.status === "completed") {
    return status.share_changed ? "running" : "current"
  }
  // A failed or interrupted run.
  if (status.status !== "ready" || !status.plan) return "pending"

  const plan = status.plan
  const toUpload = plan.new_to_cloud + plan.changed + plan.already_in_cloud + plan.losing_access
  if (toUpload > 0) return "pending"
  if (status.last_upload) return "current"
  return "never"
}

function note(state: ShareState, status: UploadStatus | undefined): string | undefined {
  switch (state) {
    case "running":
      return status?.status === "uploading" ? UPLOAD.runningNote : UPLOAD.checkingNote
    case "current":
      return UPLOAD.upToDateNote
    case "pending":
      return status?.error ?? UPLOAD.pendingNote
    case "never":
      return undefined
  }
}

/**
 * The People head's "Share network" button; it opens the upload dialog around it. Its mark: a spinner
 * while a check or upload runs, a warning when the check found people to upload, a check when the
 * network is shared and current, nothing when there is nothing to share yet.
 */
export function ShareButton({ status, busy }: { status: UploadStatus | undefined; busy: boolean }) {
  const state = shareState(status, busy)
  return (
    <div className="head-share">
      <DialogTrigger asChild>
        <Button
          aria-label={UPLOAD.share}
          aria-description={note(state, status)}
          title={note(state, status)}
          data-state={state}
        >
          {UPLOAD.share}
          {state === "running" && <Spinner />}
          {state === "current" && <CheckMark />}
          {state === "pending" && <Warning />}
        </Button>
      </DialogTrigger>
    </div>
  )
}

function CheckMark() {
  return (
    <svg className="size-3.5 shrink-0 text-ok" viewBox="0 0 16 16" aria-hidden="true">
      <path
        d="M3.5 8.5 6.5 11.5 12.5 5"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.8"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  )
}

function Warning() {
  return (
    <svg className="size-3.5 shrink-0 text-warn" viewBox="0 0 16 16" aria-hidden="true">
      <path
        d="M8 2.2 14.6 13.8H1.4z"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinejoin="round"
      />
      <path d="M8 6.4v3.4" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
      <circle cx="8" cy="11.6" r="0.9" fill="currentColor" />
    </svg>
  )
}

// One turn per --t-slow; the reduced-motion rule in index.css stops it.
function Spinner() {
  return (
    <span
      aria-hidden="true"
      className="size-3 animate-[turn_var(--t-slow)_linear_infinite] rounded-full border-2 border-current border-r-transparent"
    />
  )
}
