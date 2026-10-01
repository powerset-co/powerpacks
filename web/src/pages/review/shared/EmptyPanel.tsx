import type { ReactNode } from "react"

import { COMPLETE_MARK } from "@/lib/review/steps"
import { cn } from "@/lib/utils"

interface EmptyPanelProps {
  title: string
  /** The round check above the title (All set, the stage check). */
  mark?: boolean
  className?: string
  children?: ReactNode
}

// A centred panel with a title and whatever sits under it.
export function EmptyPanel({ title, mark = false, className, children }: EmptyPanelProps) {
  return (
    <div className={cn("empty-state", className)}>
      {mark ? (
        <span className="empty-mark" aria-hidden="true">
          {COMPLETE_MARK}
        </span>
      ) : null}
      <h2>{title}</h2>
      {children}
    </div>
  )
}
