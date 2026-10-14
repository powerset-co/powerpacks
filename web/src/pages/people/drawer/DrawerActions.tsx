import { useState } from "react"

import { BookIcon, DrawerIconTip, Spinner } from "@/components/shared"
import { Button } from "@/components/ui/button"
import { LOGBOOK } from "@/lib/people/copy"
import type { TagAction } from "@/lib/people/facets"
import type { Person } from "@/types/people"

interface DrawerActionsProps {
  row: Person
  saving: boolean
  // Saving, or the drawer is switching away from this person.
  disabled: boolean
  onAction: (action: TagAction) => void
}

const PRESSED = "flex-1 min-h-9 aria-pressed:shadow-[inset_0_0_0_1px_var(--line-strong)]"

// A chosen action's check: it rises in when a write sets it, not when the drawer opens on it.
function Check({ on, risen }: { on: boolean; risen: boolean }) {
  return on ? <span className={risen ? "rise-in" : undefined}>✓</span> : null
}

// Share and Keep private reflect the owner's own tags; Undo (toast, z) takes a choice back.
export function DrawerActions({ row, saving, disabled, onAction }: DrawerActionsProps) {
  const shares = row.tags.includes("share")
  const keepsPrivate = row.tags.includes("private")
  const [opened] = useState({ shares, keepsPrivate })
  return (
    <div className="drawer-actions" data-saving={saving || undefined}>
      <Button
        variant={shares ? "ok" : "default"}
        className={shares ? "flex-1 min-h-9" : PRESSED}
        data-one="share"
        aria-pressed={shares}
        disabled={disabled}
        onClick={() => onAction("share")}
      >
        <Check on={shares} risen={!opened.shares} />
        Share
      </Button>
      <Button
        className={PRESSED}
        data-one="private"
        aria-pressed={keepsPrivate}
        disabled={disabled}
        onClick={() => onAction("private")}
      >
        <Check on={keepsPrivate} risen={!opened.keepsPrivate} />
        Keep private
      </Button>
    </div>
  )
}

interface LogbookButtonProps {
  row: Person
  // Saving, or the drawer is switching away from this person.
  disabled: boolean
  // A logbook build is running; one runs at a time.
  building: boolean
  onBuild: () => void
  onView: () => void
}

// The logbook as an icon beside Close: View once this person has a saved one (the reader can
// refresh it), else Build, which saves their raw messages on this computer and changes nothing
// on the row.
export function LogbookButton({ row, disabled, building, onBuild, onView }: LogbookButtonProps) {
  if (row.logbook)
    return (
      <button
        type="button"
        className="drawer-icon group"
        data-logbook="saved"
        aria-label={LOGBOOK.view}
        disabled={disabled}
        onClick={onView}
      >
        <BookIcon className="size-[15px]" />
        <DrawerIconTip name={LOGBOOK.view}>{LOGBOOK.explainer}</DrawerIconTip>
      </button>
    )
  const label = building ? LOGBOOK.building : LOGBOOK.build
  return (
    <button
      type="button"
      className="drawer-icon group"
      aria-label={label}
      disabled={building || disabled}
      onClick={onBuild}
    >
      {building ? <Spinner /> : <BookIcon className="size-[15px]" />}
      <DrawerIconTip name={label}>{LOGBOOK.explainer}</DrawerIconTip>
    </button>
  )
}
