import { initials } from "@/components/shared"

// rendering.py .operator-initials: a small initials disc beside an operator's name.
export function OperatorInitials({ name }: { name: string }) {
  return (
    <span
      aria-hidden="true"
      className="inline-grid size-[18px] shrink-0 place-items-center rounded-full border border-border bg-secondary text-[9px] font-extrabold text-muted-foreground"
    >
      {initials(name)}
    </span>
  )
}
