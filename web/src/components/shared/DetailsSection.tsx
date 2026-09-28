import type { ReactNode } from "react"

import { cn } from "@/lib/utils"

import styles from "./DetailsSection.module.css"
import { Fold } from "./Fold"

interface DetailsSectionProps {
  sectionKey: string
  title: string
  count?: number
  // Beside the title in the toggle (a badge, a note).
  badge?: ReactNode
  open: boolean
  onToggle: (open: boolean) => void
  className?: string
  children: ReactNode
}

/**
 * A titled section whose body folds open and shut (Fold), both ways animated. The heading
 * holds the toggle button, the disclosure pattern; `open` is the caller's.
 */
export function DetailsSection({
  sectionKey,
  title,
  count,
  badge,
  open,
  onToggle,
  className,
  children,
}: DetailsSectionProps) {
  return (
    <section className={cn(styles.section, className)} data-section={sectionKey} data-open={open}>
      <h3 className={styles.heading}>
        <button
          type="button"
          className={cn(styles.toggle, "chevron")}
          aria-expanded={open}
          onClick={() => onToggle(!open)}
        >
          <span className={cn(styles.title, "section-title")}>
            {title}
            {count ? <small>{count.toLocaleString()}</small> : null}
          </span>
          {badge}
        </button>
      </h3>
      <Fold open={open}>
        <div className={styles.body}>{children}</div>
      </Fold>
    </section>
  )
}
