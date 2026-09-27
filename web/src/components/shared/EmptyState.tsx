import type { HTMLAttributes, ReactNode } from "react"

import { cn } from "@/lib/utils"

interface EmptyStateProps extends HTMLAttributes<HTMLParagraphElement> {
  children: ReactNode
  className?: string
}

// people.css .grid-empty: a centred muted line that rises in.
export function EmptyState({ children, className, ...rest }: EmptyStateProps) {
  return (
    <p
      {...rest}
      className={cn(
        "rise-in mx-auto my-10 max-w-[520px] text-center leading-[1.6] text-muted-foreground",
        "[&_code]:rounded [&_code]:bg-secondary [&_code]:px-1.5 [&_code]:py-0.5 [&_code]:text-xs [&_code]:text-foreground",
        className,
      )}
    >
      {children}
    </p>
  )
}
