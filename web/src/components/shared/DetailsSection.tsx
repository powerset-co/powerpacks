import type { ReactNode } from "react";

import styles from "./DetailsSection.module.css";

interface DetailsSectionProps {
  sectionKey: string;
  title: string;
  count?: number;
  badge?: ReactNode;
  open: boolean;
  onToggle: (open: boolean) => void;
  children: ReactNode;
}

export function DetailsSection({ sectionKey, title, count, badge, open, onToggle, children }: DetailsSectionProps) {
  return (
    <details
      className={styles.section}
      data-section={sectionKey}
      open={open}
      onToggle={(event) => {
        const next = event.currentTarget.open;
        if (next !== open) onToggle(next);
      }}
    >
      <summary className={styles.summary}>
        <h3 className={styles.title}>
          {title}
          {count ? <small>{count.toLocaleString()}</small> : null}
        </h3>
        {badge}
      </summary>
      {children}
    </details>
  );
}
