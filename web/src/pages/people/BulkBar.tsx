import { ActionBar, ActionBarRule, Kbd } from "@/components/shared"
import { Button } from "@/components/ui/button"
import type { TagAction } from "@/lib/people/facets"

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

// Share / Keep private for the selection or the open person, on the shared action bar.
export function BulkBar({ label, selection, saving, onAction, onClear }: BulkBarProps) {
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
      <Button variant="ghost" shape="pill" className={BAR_BUTTON} onClick={onClear}>
        {selection ? "Clear selection" : "Close"} <Kbd className="ml-0.5">Esc</Kbd>
      </Button>
    </ActionBar>
  )
}
