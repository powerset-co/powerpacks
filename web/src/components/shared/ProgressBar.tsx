interface ProgressBarProps {
  label: string
  done: number
  // 0 while the total is unknown: the bar shimmers instead of filling.
  total: number
}

/** One thin bar: filled to done/total once there is a total, a shimmer before. */
export function ProgressBar({ label, done, total }: ProgressBarProps) {
  return (
    <div
      role="progressbar"
      aria-label={label}
      aria-valuemin={0}
      aria-valuemax={total || undefined}
      aria-valuenow={total ? done : undefined}
      className="h-1.5 overflow-hidden rounded-full bg-secondary"
    >
      {total ? (
        <div
          className="h-full origin-left bg-primary transition-transform duration-med ease-out"
          style={{ transform: `scaleX(${done / total})` }}
        />
      ) : (
        <div className="h-full w-full -translate-x-full animate-[shimmer_var(--t-shimmer)_var(--ease-in)_infinite] bg-[linear-gradient(90deg,transparent,var(--primary),transparent)] motion-reduce:translate-x-0 motion-reduce:opacity-40" />
      )}
    </div>
  )
}
