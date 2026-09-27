import { useRef } from "react"

import { Kbd } from "@/components/shared"
import { Button } from "@/components/ui/button"
import { useInert } from "@/hooks/useInert"
import { usePresence } from "@/hooks/usePresence"
import type { TagAction } from "@/lib/people/facets"

import "./styles/overlays.css"

const BAR_BUTTON = "min-h-[30px]"

interface BulkBarProps {
  // "3 selected" for a selection, else the open person's name, else null: no bar.
  label: string | null
  // Whether the bar acts on a selection (Clear selection) or the open person (Close).
  selection: boolean
  saving: boolean
  onAction: (action: TagAction) => void
  onClear: () => void
}

// Share / Keep private for the selection or the open person; it rises in and drops out,
// keeping its label as it leaves. Inert whenever it is not open, exit included.
export function BulkBar({ label, selection, saving, onAction, onClear }: BulkBarProps) {
  const { mounted, open, shown, onTransitionEnd } = usePresence(label)
  const bar = useRef<HTMLDivElement>(null)

  useInert(bar, !open)

  if (!mounted || shown === null) return null
  return (
    <div
      ref={bar}
      className="bulkbar rise"
      data-bulkbar
      data-open={open}
      role="toolbar"
      aria-label={selection ? "Selection" : "Open person"}
      aria-hidden={open ? undefined : true}
      onTransitionEnd={onTransitionEnd}
    >
      <b>{shown}</b>
      <Button
        variant="ok"
        shape="pill"
        className={BAR_BUTTON}
        disabled={saving}
        onClick={() => onAction("share")}
      >
        Share <Kbd className="ml-0.5">S</Kbd>
      </Button>
      <Button shape="pill" className={BAR_BUTTON} disabled={saving} onClick={() => onAction("private")}>
        Keep private <Kbd className="ml-0.5">P</Kbd>
      </Button>
      <span className="sep" />
      <Button variant="ghost" shape="pill" className={BAR_BUTTON} onClick={onClear}>
        {selection ? "Clear selection" : "Close"} <Kbd className="ml-0.5">Esc</Kbd>
      </Button>
    </div>
  )
}
