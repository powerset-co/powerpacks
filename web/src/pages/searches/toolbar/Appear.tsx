import { useEffect, useRef, type ReactNode } from "react"

import { usePresence } from "@/hooks/usePresence"
import { cn } from "@/lib/utils"

interface AppearProps {
  show: boolean
  children: ReactNode
  className?: string
}

// Rises in when `show` turns on and drops out before unmounting (index.css .rise); inert
// while it leaves, so a leaving control takes no focus or clicks.
export function Appear({ show, children, className }: AppearProps) {
  const { mounted, open, onTransitionEnd } = usePresence(show ? true : null)
  const node = useRef<HTMLSpanElement>(null)

  useEffect(() => {
    if (node.current) node.current.inert = !open
  }, [open, mounted])

  if (!mounted) return null
  return (
    <span
      ref={node}
      data-open={open}
      aria-hidden={open ? undefined : true}
      onTransitionEnd={onTransitionEnd}
      className={cn("rise inline-flex items-center gap-1.5", className)}
    >
      {children}
    </span>
  )
}
