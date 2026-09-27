import { useEffect, useRef, type ReactNode } from "react"

import { cn } from "@/lib/utils"

import styles from "./Fold.module.css"

interface FoldProps {
  open: boolean
  className?: string
  children: ReactNode
}

/**
 * Content that folds open and shut by animating its one grid row (the one layout animation
 * the tokens allow: a variable-height block cannot fold with a transform). It stays mounted
 * and is inert while shut, so it can animate closed and takes no focus.
 */
export function Fold({ open, className, children }: FoldProps) {
  const box = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (box.current) box.current.inert = !open
  }, [open])
  return (
    <div ref={box} className={cn(styles.fold, className)} data-open={open}>
      <div className={styles.inner}>{children}</div>
    </div>
  )
}
