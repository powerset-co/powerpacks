import { cn } from "@/lib/utils"

import styles from "./SortHeader.module.css"

export type SortDirection = "ascending" | "descending" | "none"

interface SortHeaderProps {
  label: string
  sort: SortDirection
  onSort: () => void
  className?: string
}

// A sortable column header: the cell carries aria-sort, the button the label and the arrow.
export function SortHeader({ label, sort, onSort, className }: SortHeaderProps) {
  return (
    <div role="columnheader" aria-sort={sort} className={cn(styles.header, className)}>
      <button type="button" className={styles.button} onClick={onSort}>
        {label}
      </button>
    </div>
  )
}
