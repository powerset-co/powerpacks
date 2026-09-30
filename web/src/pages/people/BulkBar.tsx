import { ActionBar, ActionBarRule, Kbd } from "@/components/shared"
import { Button } from "@/components/ui/button"
import { LOGBOOK } from "@/lib/people/copy"
import type { TagAction } from "@/lib/people/facets"

const BAR_BUTTON = "min-h-[30px]"

/** A selection's logbook action: open what everyone has saved, or build it. */
export type LogbookAction = "view" | "build"

interface BulkBarProps {
  // "3 selected" for a selection, else the open person's name, else null: no bar.
  label: string | null
  // Whether the bar acts on a selection (Clear selection) or the open person (Close).
  selection: boolean
  saving: boolean
  // A logbook build is running; one runs at a time.
  building: boolean
  onAction: (action: TagAction) => void
  // The selection's logbook action; null for the open person, whose drawer has it.
  logbook: LogbookAction | null
  onLogbook: () => void
  onClear: () => void
}

// Share / Keep private for the selection or the open person, and the selection's logbook, on the shared action bar.
export function BulkBar({
  label,
  selection,
  saving,
  building,
  logbook,
  onAction,
  onLogbook,
  onClear,
}: BulkBarProps) {
  return (
    <ActionBar label={label} name={selection ? "Selection" : "Open person"}>
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
      <ActionBarRule />
      {logbook === "view" ? (
        <Button shape="pill" className={BAR_BUTTON} onClick={onLogbook}>
          {LOGBOOK.view}
        </Button>
      ) : null}
      {logbook === "build" ? (
        <Button
          shape="pill"
          className={BAR_BUTTON}
          title={LOGBOOK.explainer}
          disabled={building}
          onClick={onLogbook}
        >
          {building ? LOGBOOK.building : LOGBOOK.build}
        </Button>
      ) : null}
      {logbook ? <ActionBarRule /> : null}
      <Button variant="ghost" shape="pill" className={BAR_BUTTON} onClick={onClear}>
        {selection ? "Clear selection" : "Close"} <Kbd className="ml-0.5">Esc</Kbd>
      </Button>
    </ActionBar>
  )
}
