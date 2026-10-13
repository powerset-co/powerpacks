import { useState } from "react"

import { cn } from "@/lib/utils"

import { initials } from "./initials"

interface AvatarProps {
  name: string | null
  // Their picture (the signed-in footer's); the initials show while it is missing or won't load.
  src?: string | null
  className?: string
}

// A person's picture or initials on a disc; with no name, the anonymous silhouette (the signed-out
// footer).
export function Avatar({ name, src, className }: AvatarProps) {
  const [broken, setBroken] = useState(false)
  return (
    <span
      aria-hidden
      className={cn(
        "grid size-7 shrink-0 place-items-center overflow-hidden rounded-full border border-border bg-secondary text-[11px] font-extrabold text-muted-foreground",
        name === null && "border-dashed text-faint",
        className,
      )}
    >
      {src && !broken ? (
        <img
          src={src}
          alt=""
          referrerPolicy="no-referrer"
          className="size-full object-cover"
          onError={() => setBroken(true)}
        />
      ) : name === null ? (
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
