import type { SelectHTMLAttributes } from "react"

import { cn } from "@/lib/utils"

// A native select in the app's field style, with a chevron.
export function Select({ className, children, ...props }: SelectHTMLAttributes<HTMLSelectElement>) {
  return (
    <span className="relative inline-flex">
      <select
        className={cn(
          "h-8 cursor-pointer appearance-none rounded-[var(--radius-s)] border border-line-strong bg-background pl-2.5 pr-8 text-[13px] text-foreground disabled:cursor-default disabled:text-muted-foreground",
          className,
        )}
        {...props}
      >
        {children}
      </select>
      <svg
        viewBox="0 0 16 16"
        aria-hidden="true"
        className="pointer-events-none absolute right-2.5 top-1/2 size-3 -translate-y-1/2 text-muted-foreground"
      >
        <path
          d="M4 6l4 4 4-4"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.6"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      </svg>
    </span>
  )
}
