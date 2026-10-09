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
  onOpen: () => void
}

/** Never shared: Share network; shared and unchanged since: Shared network; edited since: Update network. */
function label(status: UploadStatus | undefined, busy: boolean): string {
  if (busy) return UPLOAD.view
  if (!status?.last_upload) return UPLOAD.share
  return status.share_changed ? UPLOAD.update : UPLOAD.shared
}

// The head's share control: the button opens the upload (the trigger of the dialog around it); the caret
// opens the set menu: the personal (local) network and the cloud sets, the one shared to checked, and
// "Manage sets…", which opens the Sets page.
export function ShareMenu({ status, busy, onOpen }: ShareMenuProps) {
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
  const shown = sets.data?.sets ?? []
  const cloud = shown.filter((set) => !set.is_personal)
  const chosen = cloud.find((set) => set.set_id === target)
  return (
    <div className="head-share share-menu" ref={box}>
      <div className="share-split">
        <DialogTrigger asChild>
          <Button className="share-main" aria-label={label(status, busy)} onClick={onOpen}>
            {busy && <Spinner />}
            {label(status, busy)}
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

function Check({ on }: { on: boolean }) {
  return (
    <svg className={cn("share-check", on && "on")} viewBox="0 0 16 16" aria-hidden="true">
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

// One turn per --t-slow; the reduced-motion rule in index.css stops it.
function Spinner() {
  return (
    <span
      aria-hidden="true"
      className="size-3 animate-[turn_var(--t-slow)_linear_infinite] rounded-full border-2 border-current border-r-transparent"
    />
  )
}
