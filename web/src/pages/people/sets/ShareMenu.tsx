import { useEffect, useRef, useState } from "react"
import { useNavigate } from "react-router-dom"
import { Button } from "@/components/ui/button"
import { DialogTrigger } from "@/components/ui/dialog"
import type { UploadStatus } from "@/lib/api/upload"
import { SETS, UPLOAD } from "@/lib/people/copy"
import { cn } from "@/lib/utils"
import { SetIcon, HomeIcon } from "./icons"
import { readTarget, useSets, writeTarget } from "./useSets"
import "../styles/sets.css"

interface ShareMenuProps {
  status: UploadStatus | undefined
  busy: boolean
}

/** Always "Share network"; the mark says where it stands, from the check of the current share list:
 * a spinner while it (or an upload) runs, a warning when it found something to upload, a check when the
 * network is shared and current. Nothing to upload and never shared: no mark. */
type ShareState = "running" | "never" | "current" | "pending"

function shareState(status: UploadStatus | undefined, busy: boolean): ShareState {
  if (busy || !status || status.status === "idle") return "running"
  if (status.status === "completed") return status.share_changed ? "running" : "current"
  if (status.status !== "ready" || !status.plan) return "pending"
  const plan = status.plan
  if (plan.new_to_cloud + plan.changed + plan.already_in_cloud + plan.losing_access > 0) return "pending"
  return status.last_upload ? "current" : "never"
}

function note(state: ShareState, status: UploadStatus | undefined): string | undefined {
  if (state === "running") return status?.status === "uploading" ? UPLOAD.runningNote : UPLOAD.checkingNote
  if (state === "current") return UPLOAD.upToDateNote
  if (state === "pending") return status?.error ?? UPLOAD.pendingNote
  return undefined
}

// The head's share control: the button opens the upload (the trigger of the dialog around it); the caret
// opens the set menu: the personal (local) network and the cloud sets, the one shared to checked, and
// "Manage sets…", which opens the Sets page.
export function ShareMenu({ status, busy }: ShareMenuProps) {
  const sets = useSets()
  const [open, setOpen] = useState(false)
  const navigate = useNavigate()
  const [target, setTarget] = useState(readTarget)
  const box = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!open) return
    const away = (event: MouseEvent) => {
      if (event.target instanceof Node && box.current?.contains(event.target)) return
      setOpen(false)
    }
    const key = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false)
    }
    document.addEventListener("mousedown", away)
    document.addEventListener("keydown", key)
    return () => {
      document.removeEventListener("mousedown", away)
      document.removeEventListener("keydown", key)
    }
  }, [open])
  const pick = (set_id: string) => {
    setTarget(set_id)
    writeTarget(set_id)
    setOpen(false)
  }
  const state = shareState(status, busy)
  const shown = sets.data?.sets ?? []
  const cloud = shown.filter((set) => !set.is_personal)
  const chosen = cloud.find((set) => set.set_id === target)
  return (
    <div className="head-share share-menu" ref={box}>
      <div className="share-split">
        <DialogTrigger asChild>
          <Button
            className="share-main"
            aria-label={UPLOAD.share}
            aria-description={note(state, status)}
            title={note(state, status)}
            data-state={state}
          >
            {UPLOAD.share}
            {state === "running" && <Spinner />}
            {state === "current" && <Check on className="shared" />}
            {state === "pending" && <Warning />}
          </Button>
        </DialogTrigger>
        <Button
          className="share-caret chevron"
          aria-label={SETS.choose}
          aria-expanded={open}
          aria-haspopup="menu"
          onClick={() => setOpen(!open)}
        />
      </div>
      {open ? (
        <div className="share-popover" role="menu" aria-label={SETS.choose}>
          <p className="share-popover-title">{SETS.shareTo}</p>
          <button
            type="button"
            role="menuitemradio"
            aria-checked={!chosen}
            className={cn("share-option", !chosen && "picked")}
            onClick={() => pick("")}
          >
            <HomeIcon className="share-option-icon local" />
            <span className="share-option-text">
              <span className="share-option-name">{SETS.personalNetwork}</span>
              <span className="share-option-note">
                {SETS.local}
                {sets.data ? ` · ${SETS.shared(sets.data.shared)}` : ""}
              </span>
            </span>
            <Check on={!chosen} />
          </button>
          {cloud.map((set) => (
            <button
              key={set.set_id}
              type="button"
              role="menuitemradio"
              aria-checked={chosen?.set_id === set.set_id}
              className={cn("share-option", chosen?.set_id === set.set_id && "picked")}
              onClick={() => pick(set.set_id)}
            >
              <SetIcon className="share-option-icon" />
              <span className="share-option-text">
                <span className="share-option-name">{set.name}</span>
                <span className="share-option-note">{SETS.members(set.member_count)}</span>
              </span>
              <Check on={chosen?.set_id === set.set_id} />
            </button>
          ))}
          {sets.failure ? (
            <p className="share-popover-note">
              {sets.failure.needsAuth ? SETS.signInNeeded : sets.failure.message}
            </p>
          ) : null}
          <div className="share-popover-rule" />
          <button
            type="button"
            role="menuitem"
            className="share-option manage"
            onClick={() => {
              setOpen(false)
              void navigate(chosen ? `/sets?set=${encodeURIComponent(chosen.set_id)}` : "/sets")
            }}
          >
            {SETS.manage}
          </button>
        </div>
      ) : null}
    </div>
  )
}

function Check({ on, className }: { on: boolean; className?: string }) {
  return (
    <svg className={cn("share-check", on && "on", className)} viewBox="0 0 16 16" aria-hidden="true">
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
