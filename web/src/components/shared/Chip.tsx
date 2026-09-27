import type { ButtonHTMLAttributes, ReactNode } from "react"

import { cn } from "@/lib/utils"

interface ChipProps extends Omit<ButtonHTMLAttributes<HTMLButtonElement>, "aria-pressed"> {
  pressed: boolean
  // A faint leading name, as in "Worth yes".
  label?: string
  count?: number
  // The chip removes itself when pressed: a trailing × that lights on hover.
  removable?: boolean
  children: ReactNode
}

// results.css .chip: a filter pill with an optional count. Pressing only changes colour;
// press movement belongs to Button alone (its 1px drop), never a scale.
export function Chip({ pressed, label, count, removable = false, children, className, ...props }: ChipProps) {
  return (
    <button
      type="button"
      aria-pressed={pressed}
      className={cn(
        "group inline-flex min-h-7 cursor-pointer items-center gap-1.5 rounded-full border border-border bg-transparent px-2.5 py-1 text-[11.5px] font-semibold leading-none text-muted-foreground transition-[color,border-color,background-color] duration-fast ease-out",
        "hover:border-line-strong hover:text-foreground",
        "aria-pressed:border-primary aria-pressed:bg-primary-soft aria-pressed:text-foreground",
        "disabled:cursor-default disabled:opacity-40 disabled:hover:border-border disabled:hover:text-muted-foreground",
        removable && "pr-1.5",
        className,
      )}
      {...props}
    >
      {label === undefined ? null : <em className="font-semibold not-italic text-faint">{label}</em>}
      {children}
      {count === undefined ? null : (
        <span className="font-semibold tabular-nums text-faint group-aria-pressed:text-muted-foreground">
          {count.toLocaleString()}
        </span>
      )}
      {removable ? (
        <span
          aria-hidden="true"
          className="inline-grid size-4 place-items-center rounded-full text-muted-foreground transition-colors duration-fast ease-out group-hover:bg-secondary group-hover:text-foreground"
        >
          ×
        </span>
      ) : null}
    </button>
  )
}
