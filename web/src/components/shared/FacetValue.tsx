import type { ButtonHTMLAttributes } from "react";

import styles from "./FacetValue.module.css";

type Passthrough = Omit<ButtonHTMLAttributes<HTMLButtonElement>, "type" | "className" | "disabled" | "onClick" | "aria-pressed">;

interface FacetValueProps extends Passthrough {
  label: string;
  count: number;
  pressed: boolean;
  onToggle: () => void;
}

// A value with no people left is disabled unless it is already held, so it can be released.
export function FacetValue({ label, count, pressed, onToggle, ...rest }: FacetValueProps) {
  return (
    <button
      {...rest}
      type="button"
      className={styles.value}
      aria-pressed={pressed}
      disabled={count === 0 && !pressed}
      onClick={onToggle}
    >
      <span className={styles.label}>{label}</span>
      <span className={styles.count}>{count.toLocaleString()}</span>
    </button>
  );
}
