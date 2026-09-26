import type { ReactNode } from "react";

import styles from "./FacetShell.module.css";

interface FacetShellProps {
  facetKey: string;
  label: string;
  open: boolean;
  active: boolean;
  onToggle: () => void;
  children: ReactNode;
}

export function FacetShell({ facetKey, label, open, active, onToggle, children }: FacetShellProps) {
  return (
    <div className={styles.facet} data-facet={facetKey} data-open={open}>
      <button type="button" className={styles.head} aria-expanded={open} onClick={onToggle}>
        {label}
        {active ? <i className={styles.dot} aria-hidden="true" /> : null}
      </button>
      <div className={styles.body}>
        <div className={styles.list}>{children}</div>
      </div>
    </div>
  );
}
