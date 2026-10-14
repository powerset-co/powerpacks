import type { ReactNode } from "react"

import { cn } from "@/lib/utils"

// Where the label sits: at the side nav's bottom-left corner, where the window's edges are near,
// above the control (left-aligned) or to its right (bottom-aligned); elsewhere below it.
const SIDE = {
  above: "bottom-[calc(100%+8px)] left-0",
  right: "bottom-0 left-[calc(100%+10px)]",
  below: "top-[calc(100%+6px)] left-0",
} as const

// A hover label for a `group` control.
export function Tip({
  side,
  children,
  id,
  className,
}: {
  side: keyof typeof SIDE
  children: ReactNode
  id?: string
  className?: string
}) {
  return (
    <span
      id={id}
      role="tooltip"
      className={cn(
        "pointer-events-none absolute z-10 rounded-md border border-line bg-[var(--background)] px-2.5 py-1.5 text-left text-[12px] font-medium tracking-normal whitespace-nowrap text-foreground opacity-0 shadow-sm transition-opacity duration-100 group-hover:opacity-100 group-focus-visible:opacity-100",
        SIDE[side],
        className,
      )}
    >
      {children}
    </span>
  )
}
