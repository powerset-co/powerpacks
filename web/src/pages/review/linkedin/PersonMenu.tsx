import { useEffect, useRef, useState, type RefObject } from "react"

import { MENU } from "./copy"

interface PersonMenuProps {
  /** The menu's box: the feedback popover hangs under it. */
  anchor: RefObject<HTMLDivElement>
  /** A decision is saving. */
  disabled: boolean
  /** "Leave feedback" was picked. */
  onFeedback: () => void
}

// The `person_menu` macro: the "⋯" in the card's corner and its one item. A click anywhere
// else on the page shuts an open menu.
export function PersonMenu({ anchor, disabled, onFeedback }: PersonMenuProps) {
  const [open, setOpen] = useState(false)
  const toggle = useRef<HTMLButtonElement>(null)

  useEffect(() => {
    if (!open) return
    const shut = (event: MouseEvent) => {
      if (event.target instanceof Node && toggle.current?.contains(event.target)) return
      setOpen(false)
    }
    document.addEventListener("click", shut)
    return () => document.removeEventListener("click", shut)
  }, [open])

  return (
    <div className="card-menu person-menu" ref={anchor}>
      <button
        ref={toggle}
        type="button"
        className="button button-outline person-menu-toggle"
        aria-label={MENU.toggle}
        disabled={disabled}
        onClick={() => setOpen(!open)}
      >
        {MENU.mark}
      </button>
      <div className="person-menu-items" hidden={!open}>
        <button
          type="button"
          className="button button-ghost"
          disabled={disabled}
          onClick={() => {
            setOpen(false)
            onFeedback()
          }}
        >
          {MENU.feedback}
        </button>
      </div>
    </div>
  )
}
