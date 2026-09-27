import { PinIcon } from "@/components/shared"
import { cn } from "@/lib/utils"

interface PinButtonProps {
  name: string
  pinned: boolean
  // While the saved tags load: a pin then would overwrite them.
  disabled: boolean
  onToggle: () => void
}

// results.js pin-trigger: puts the "Pinned" tag on the person, or takes it off. Shown on the
// row's hover or focus, and always once pinned.
export function PinButton({ name, pinned, disabled, onToggle }: PinButtonProps) {
  return (
    <button
      type="button"
      data-row-action="pin"
      aria-pressed={pinned}
      aria-label={`${pinned ? "Unpin" : "Pin"} ${name}`}
      title={pinned ? "Remove from shortlist" : "Pin to shortlist"}
      disabled={disabled}
      className={cn(
        "inline-grid size-7 shrink-0 cursor-pointer place-items-center rounded-full text-muted-foreground opacity-0 transition-[color,background-color,opacity] duration-fast ease-out hover:bg-secondary hover:text-foreground focus-visible:opacity-100 disabled:cursor-default group-hover/row:opacity-100 aria-pressed:text-primary",
        pinned && "opacity-100",
      )}
      onClick={onToggle}
    >
      <PinIcon
        className={cn(
          "size-3.5 transition-[fill] duration-fast ease-out",
          pinned ? "fill-current" : "fill-none",
        )}
      />
    </button>
  )
}
