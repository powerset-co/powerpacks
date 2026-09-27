import { useState } from "react"

import { Fold, Kbd } from "@/components/shared"

const SHORTCUTS: readonly (readonly [readonly string[], string])[] = [
  [["1", "2", "3"], "Switch tab"],
  [["/"], "Search"],
  [["J", "K"], "Move"],
  [["X"], "Select"],
  [["⇧A"], "Select all matching"],
  [["S"], "Share"],
  [["P"], "Keep private"],
  [["W"], "Use worth"],
  [["Z"], "Undo"],
  [["Enter"], "Open or close details"],
]

// The page's keys under a toggle; the list folds open and shut.
export function ShortcutsHint() {
  const [open, setOpen] = useState(false)
  return (
    <div className="rail-hint" data-hint>
      <button type="button" aria-expanded={open} onClick={() => setOpen(!open)}>
        Keyboard shortcuts
      </button>
      <Fold open={open}>
        <dl>
          {SHORTCUTS.map(([keys, action]) => (
            <div key={action} className="contents">
              <dt>
                {keys.map((key) => (
                  <Kbd key={key}>{key}</Kbd>
                ))}
              </dt>
              <dd>{action}</dd>
            </div>
          ))}
        </dl>
      </Fold>
    </div>
  )
}
