import { useRef, type ReactNode } from "react"

import { useInert } from "@/hooks/useInert"
import { usePresence } from "@/hooks/usePresence"
import { cn } from "@/lib/utils"

import "./ActionBar.css"

interface ActionBarProps {
  // What the bar acts on ("3 selected", a person's name), or null: no bar.
  label: string | null
  // The toolbar's accessible name.
  name: string
  className?: string
  children: ReactNode
}

// A pill of actions floating at the bottom: it rises in and drops out, keeping its label as it
// leaves. Inert whenever it is not open, exit included.
export function ActionBar({ label, name, className, children }: ActionBarProps) {
  const { mounted, open, shown, onTransitionEnd } = usePresence(label)
  const bar = useRef<HTMLDivElement>(null)

  useInert(bar, !open)

  if (!mounted || shown === null) return null
  return (
    <div
      ref={bar}
      className={cn("action-bar rise", className)}
      data-action-bar
      data-open={open}
      role="toolbar"
      aria-label={name}
      aria-hidden={open ? undefined : true}
      onTransitionEnd={onTransitionEnd}
    >
      <b>{shown}</b>
      {children}
    </div>
  )
}

// The rule between groups of the bar's buttons.
export function ActionBarRule() {
  return <span className="action-bar-rule" />
}
