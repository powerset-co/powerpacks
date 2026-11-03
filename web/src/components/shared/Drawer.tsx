import { useEffect, useRef, type ReactNode, type TransitionEvent } from "react"

import { useInert } from "@/hooks/useInert"

import "./Drawer.css"
import { CLOSE_MARK } from "./icons/actions"
import { useStoredSize } from "@/hooks/useStoredSize"

import { ResizeHandle } from "./ResizeHandle"
import { Tip } from "./Tip"

interface DrawerProps {
  open: boolean
  label: string
  // Keys the content (hooks/useDrawerSwap `shownId`); null draws an empty panel.
  contentKey: string | null
  leaving: boolean
  onTransitionEnd: (event: TransitionEvent<HTMLElement>) => void
  children: ReactNode
}

// The drawer's width as last dragged (every drawer shares it), and its bounds: never narrower than
// the stylesheet's, never wider than 80% of the window.
const WIDTH_KEY = "drawer.width"
const DEFAULT_WIDTH = 440
const MIN_WIDTH = 360

function bounded(width: number): number {
  return Math.round(Math.max(MIN_WIDTH, Math.min(window.innerWidth * 0.8, width)))
}

/**
 * A fixed, non-modal panel over the right edge; the page under it keeps its geometry. It
 * slides in over --t-slow and out over --t-med, inert while shut. Its content is keyed, so a
 * new item fades in from @starting-style; `leaving` fades the old one out first. A new item
 * starts at the top. Its left edge drags it wider; the width sets --drawer-w on the page, so what
 * centres beside the drawer follows.
 */
export function Drawer({ open, label, contentKey, leaving, onTransitionEnd, children }: DrawerProps) {
  const panel = useRef<HTMLElement>(null)

  useEffect(() => {
    if (panel.current) panel.current.scrollTop = 0
  }, [contentKey])

  useInert(panel, !open)

  const [width, setWidth] = useStoredSize(WIDTH_KEY, DEFAULT_WIDTH)
  useEffect(() => {
    document.documentElement.style.setProperty("--drawer-w", `${bounded(width)}px`)
  }, [width])

  return (
    <aside
      ref={panel}
      className="drawer"
      data-drawer
      data-open={open}
      aria-hidden={open ? undefined : true}
      aria-label={label}
    >
      <ResizeHandle
        label="Resize details"
        edge="left"
        onDrag={(clientX) => setWidth(bounded(window.innerWidth - clientX))}
        onStep={(direction) => setWidth(bounded(width + direction * 24))}
        onReset={() => setWidth(DEFAULT_WIDTH)}
        // The drawer scrolls; pinned to the window, its edge stays put. (Open, the drawer has no
        // transform, so fixed is the window's.)
        style={{ position: "fixed", left: "auto", right: "calc(min(var(--drawer-w), 100vw) - 4px)" }}
      />
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

// What an icon beside Close does, on hover: its name, then a line on what it does. The icon's
// button carries `group`.
export function DrawerIconTip({ name, children }: { name: string; children: ReactNode }) {
  return (
    <Tip side="below-left" className="w-60 whitespace-normal leading-relaxed">
      <span className="block">{name}</span>
      <span className="block font-normal text-muted-foreground">{children}</span>
    </Tip>
  )
}
