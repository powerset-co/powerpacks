import { useEffect, useRef, type ReactNode, type TransitionEvent } from "react"

import { useInert } from "@/hooks/useInert"

import "./Drawer.css"
import { CLOSE_MARK } from "./icons/actions"

interface DrawerProps {
  open: boolean
  label: string
  // Keys the content (hooks/useDrawerSwap `shownId`); null draws an empty panel.
  contentKey: string | null
  leaving: boolean
  onTransitionEnd: (event: TransitionEvent<HTMLElement>) => void
  children: ReactNode
}

/**
 * A fixed, non-modal panel over the right edge; the page under it keeps its geometry. It
 * slides in over --t-slow and out over --t-med, inert while shut. Its content is keyed, so a
 * new item fades in from @starting-style; `leaving` fades the old one out first. A new item
 * starts at the top.
 */
export function Drawer({ open, label, contentKey, leaving, onTransitionEnd, children }: DrawerProps) {
  const panel = useRef<HTMLElement>(null)

  useEffect(() => {
    if (panel.current) panel.current.scrollTop = 0
  }, [contentKey])

  useInert(panel, !open)

  return (
    <aside
      ref={panel}
      className="drawer"
      data-drawer
      data-open={open}
      aria-hidden={open ? undefined : true}
      aria-label={label}
    >
      {contentKey === null ? null : (
        <div
          key={contentKey}
          className="drawer-inner"
          data-leaving={leaving || undefined}
          onTransitionEnd={onTransitionEnd}
        >
          {children}
        </div>
      )}
    </aside>
  )
}

// The round close button at the end of a drawer's header row (.drawer-top).
export function DrawerClose({ onClose }: { onClose: () => void }) {
  return (
    <button
      type="button"
      className="drawer-close"
      data-drawer-close
      aria-label="Close details"
      onClick={onClose}
    >
      {CLOSE_MARK}
    </button>
  )
}
