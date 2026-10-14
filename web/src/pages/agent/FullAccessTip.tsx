import { CLOSE_MARK } from "@/components/shared"

interface FullAccessTipProps {
  onEnable: () => void
  onDismiss: () => void
}

/** Nudges toward the Full access switch (lib/agent/fullAccessTip decides whether it shows). */
export function FullAccessTip({ onEnable, onDismiss }: FullAccessTipProps) {
  return (
    <aside
      aria-label="Tip"
      className="rise-in flex items-center gap-3 rounded-[var(--radius-m)] border border-line bg-surface-2 py-2 pl-3.5 pr-2 text-xs text-muted-foreground"
    >
      <p className="m-0 min-w-0 flex-1">
        Tired of approving? Turn on Full access and Codex runs commands without asking.
      </p>
      <button
        type="button"
        onClick={onEnable}
        className="shrink-0 cursor-pointer rounded-full border border-line-strong bg-card px-2.5 py-1 text-[11px] font-semibold text-foreground transition-colors duration-fast ease-out hover:bg-line-strong"
      >
        Turn on Full access
      </button>
      <button
        type="button"
        aria-label="Dismiss"
        onClick={onDismiss}
        className="grid size-7 shrink-0 cursor-pointer place-items-center rounded-[var(--radius-s)] border-0 bg-transparent text-[18px] leading-none text-faint hover:bg-secondary hover:text-foreground"
      >
        {CLOSE_MARK}
      </button>
    </aside>
  )
}
