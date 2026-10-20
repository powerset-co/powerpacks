import { ActionBar, ActionBarRule, Kbd } from "@/components/shared"
import { Button } from "@/components/ui/button"
import { LOGBOOK } from "@/lib/people/copy"
import type { WorthAction } from "@/lib/people/facets"

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
  onAction: (action: WorthAction) => void
  // The delete question while one is armed ("Delete 3 people?"): the bar asks it in place.
  question: string | null
  onDelete: () => void
  onConfirmDelete: () => void
  onCancelDelete: () => void
  onClear: () => void
}

// Keep / Skip for the selection or the open person, on the shared action bar.
export function BulkBar({
  label,
  selection,
  disabled,
  building,
  logbook,
  onAction,
  question,
  onDelete,
  onConfirmDelete,
  onCancelDelete,
  onLogbook,
  onClear,
}: BulkBarProps) {
  if (question !== null && label !== null) {
    return (
      <ActionBar label={question} name="Confirm delete" className="confirming">
        <span key="confirm" className="action-bar-group rise-in">
          <span className="action-bar-note">Their messages are not touched.</span>
          <Button
            variant="bad"
            shape="pill"
            className={BAR_BUTTON}
            disabled={disabled}
            onClick={onConfirmDelete}
          >
            Delete <Kbd className="ml-0.5">↵</Kbd>
          </Button>
          <Button variant="ghost" shape="pill" className={BAR_BUTTON} onClick={onCancelDelete}>
            Cancel <Kbd className="ml-0.5">Esc</Kbd>
          </Button>
        </span>
      </ActionBar>
    )
  }
  return (
    <ActionBar label={label} name={selection ? "Selection" : "Open person"}>
      <Button
        variant="ok"
        shape="pill"
        className={BAR_BUTTON}
        disabled={disabled}
        onClick={() => onAction("yes")}
      >
        Keep <Kbd className="ml-0.5">Y</Kbd>
      </Button>
      <Button shape="pill" className={BAR_BUTTON} disabled={disabled} onClick={() => onAction("no")}>
        Skip <Kbd className="ml-0.5">N</Kbd>
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
      <Button variant="bad" shape="pill" className={BAR_BUTTON} disabled={disabled} onClick={onDelete}>
        Delete <Kbd className="ml-0.5">D</Kbd>
      </Button>
      <ActionBarRule />
      <Button variant="ghost" shape="pill" className={BAR_BUTTON} onClick={onClear}>
        {selection ? "Clear selection" : "Close"} <Kbd className="ml-0.5">Esc</Kbd>
      </Button>
    </ActionBar>
  )
}
