import type { ReactNode } from "react"

import styles from "./FacetShell.module.css"
import { Fold } from "./Fold"

interface FacetShellProps {
  facetKey: string
  label: string
  open: boolean
  active: boolean
  onToggle: () => void
  children: ReactNode
}

// A facet: its head with the chevron and the active dot, and its values in a Fold.
export function FacetShell({ facetKey, label, open, active, onToggle, children }: FacetShellProps) {
  return (
    <div className={styles.facet} data-facet={facetKey} data-open={open}>
      <button type="button" className={styles.head} aria-expanded={open} onClick={onToggle}>
        {label}
        <i className={styles.dot} data-active={active} aria-hidden="true" />
      </button>
      <Fold open={open}>{children}</Fold>
    </div>
  )
}
