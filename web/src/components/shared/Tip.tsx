import type { ReactNode } from "react"

import { cn } from "@/lib/utils"

// A hover label for a `group` control at the side nav's bottom-left corner, where the window's
// edges are near: above the control (left-aligned) or to its right (bottom-aligned).
export function Tip({ side, children }: { side: "above" | "right"; children: ReactNode }) {
  return (
    <span
      role="tooltip"
      className={cn(
        "pointer-events-none absolute z-10 rounded-md border border-line bg-[var(--background)] px-2.5 py-1.5 text-[12px] font-medium tracking-normal whitespace-nowrap text-foreground opacity-0 shadow-sm transition-opacity duration-100 group-hover:opacity-100 group-focus-visible:opacity-100",
        side === "above" ? "bottom-[calc(100%+8px)] left-0" : "bottom-0 left-[calc(100%+10px)]",
      )}
    >
      {children}
    </span>
  )
}
