import { ActionBar, ActionBarRule, Kbd } from "@/components/shared"
import { Button } from "@/components/ui/button"
import { LOGBOOK } from "@/lib/people/copy"
import type { TagAction } from "@/lib/people/facets"

const BAR_BUTTON = "min-h-[30px]"

/** Open the selection's saved logbooks, or build them. */
export type LogbookAction = "view" | "build"

interface BulkBarProps {
  // "3 selected" for a selection, else the open person's name, else null: no bar.
  label: string | null
  // Whether the bar acts on a selection (Clear selection) or the open person (Close).
  selection: boolean
  disabled: boolean
  building: boolean
  logbook: LogbookAction | null
  onLogbook: () => void
  onAction: (action: TagAction) => void
  onClear: () => void
}

// Share / Keep private for the selection or the open person, on the shared action bar.
export function BulkBar({
  label,
  selection,
  disabled,
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
        disabled={disabled}
        onClick={() => onAction("share")}
      >
        Share <Kbd className="ml-0.5">S</Kbd>
      </Button>
      <Button shape="pill" className={BAR_BUTTON} disabled={disabled} onClick={() => onAction("private")}>
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
