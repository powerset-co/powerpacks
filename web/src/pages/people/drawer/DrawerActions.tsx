import { useState } from "react"

import { Button } from "@/components/ui/button"
import { USE_WORTH_HELP } from "@/lib/people/copy"
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

// Share and Keep private reflect the owner's own tags; Use worth removes that choice.
export function DrawerActions({ row, saving, disabled, onAction }: DrawerActionsProps) {
  const shares = row.tags.includes("share")
  const keepsPrivate = row.tags.includes("private")
  const chosen = shares || keepsPrivate
  const [opened] = useState({ shares, keepsPrivate })
  return (
    <>
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
      <div className="drawer-worth">
        <Button
          variant="ghost"
          className="min-h-7 px-2"
          data-one="worth"
          aria-pressed={!chosen}
          disabled={disabled || !chosen}
          onClick={() => onAction("worth")}
        >
          Use worth
        </Button>
        <span>{saving ? "Saving…" : `${USE_WORTH_HELP}.`}</span>
      </div>
    </>
  )
}
