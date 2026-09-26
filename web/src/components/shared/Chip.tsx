import type { ButtonHTMLAttributes, ReactNode } from "react";

import { cn } from "@/lib/utils";

interface ChipProps extends Omit<ButtonHTMLAttributes<HTMLButtonElement>, "aria-pressed"> {
  pressed: boolean;
  count?: number;
  children: ReactNode;
}

// results.css .chip: a filter pill with an optional count. No press scaling (polish B).
export function Chip({ pressed, count, children, className, ...props }: ChipProps) {
  return (
    <button
      type="button"
      aria-pressed={pressed}
      className={cn(
        "group inline-flex min-h-7 cursor-pointer items-center gap-1.5 rounded-full border border-border bg-transparent px-2.5 py-1 text-[11.5px] font-semibold leading-none text-muted-foreground transition-[color,border-color,background-color] duration-fast ease-out",
        "hover:border-line-strong hover:text-foreground",
        "aria-pressed:border-primary aria-pressed:bg-primary-soft aria-pressed:text-foreground",
        "disabled:cursor-default disabled:opacity-40 disabled:hover:border-border disabled:hover:text-muted-foreground",
        className,
      )}
      {...props}
    >
      {children}
      {count === undefined ? null : (
        <span className="font-semibold tabular-nums text-faint group-aria-pressed:text-muted-foreground">
          {count.toLocaleString()}
        </span>
      )}
    </button>
  );
}
