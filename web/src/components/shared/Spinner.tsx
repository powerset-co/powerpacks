import { cn } from "@/lib/utils"

/** A small ring in the current text colour while something runs; its host carries the words. */
export function Spinner({ className }: { className?: string }) {
  return (
    <span
      aria-hidden
      className={cn(
        "inline-block size-3 shrink-0 animate-spin rounded-full border-[1.5px] border-current border-t-transparent",
        className,
      )}
    />
  )
}
