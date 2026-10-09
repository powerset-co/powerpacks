import { cn } from "@/lib/utils"

import { initials } from "./initials"

// A person's initials on a disc; with no name, the anonymous silhouette (the signed-out footer).
export function Avatar({ name, className }: { name: string | null; className?: string }) {
  return (
    <span
      aria-hidden
      className={cn(
        "grid size-7 shrink-0 place-items-center rounded-full border border-border bg-secondary text-[11px] font-extrabold text-muted-foreground",
        name === null && "border-dashed text-faint",
        className,
      )}
    >
      {name === null ? (
        <svg viewBox="0 0 24 24" fill="currentColor" className="size-4">
          <circle cx="12" cy="8.5" r="3.5" />
          <path d="M5 19.5a7 7 0 0 1 14 0z" />
        </svg>
      ) : (
        initials(name)
      )}
    </span>
  )
}
