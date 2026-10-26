import type { ReactNode } from "react"
import { ChevronRight } from "lucide-react"

import { CheckIcon, Spinner } from "@/components/shared"
import { Button } from "@/components/ui/button"
import { cn } from "@/lib/utils"

export type ConnectionState = "checking" | "working" | "ready" | "needed"

// Every row's right side: one slot of one width, an icon beside a word.
const SLOT = "w-28 shrink-0 self-center"

/** One thing to connect: what it is for, then its slot: Connect while it is not connected,
 *  "Working" while its sign-in runs, a check once connected. */
export function ConnectionRow({
  icon,
  title,
  why,
  state,
  action,
  children,
  details,
  onIcon,
}: {
  icon: ReactNode
  title: ReactNode
  why: ReactNode
  state: ConnectionState
  action?: { label: string; onClick: () => void; disabled?: boolean }
  // What the step says while it works or stopped: its line, a code to scan, a command.
  children?: ReactNode
  details?: ReactNode
  /** With this, the icon is a button: the onboarding shows how this one connects. */
  onIcon?: () => void
}) {
  const badge = "grid size-9 shrink-0 place-items-center rounded-[var(--radius-s)] bg-surface-2 text-foreground"
  const row = (
    <>
      {onIcon ? (
        <button
          type="button"
          aria-label={typeof title === "string" ? `How ${title} connects` : "How this connects"}
          className={cn(badge, "cursor-pointer border-0 p-0 hover:bg-secondary")}
          onClick={onIcon}
        >
          {icon}
        </button>
      ) : (
        <span className={badge}>{icon}</span>
      )}
      <div className="flex min-w-0 flex-1 flex-col gap-1">
        <span className="text-[13px] font-bold">{title}</span>
        <span className="text-xs leading-snug text-muted-foreground">{why}</span>
        {children}
      </div>
      {state === "needed" && action ? (
        <Button
          size="sm"
          onClick={action.onClick}
          disabled={action.disabled}
          aria-label={typeof title === "string" ? `${title}: ${action.label}` : action.label}
          className={SLOT}
        >
          {action.label}
        </Button>
      ) : (
        <span
          className={cn(
            SLOT,
            "rise-in inline-flex items-center justify-center gap-1.5 text-xs font-semibold",
            state === "ready" ? "text-ok" : "text-muted-foreground",
          )}
        >
          {state === "ready" ? <CheckIcon className="size-3.5" /> : state !== "needed" ? <Spinner /> : null}
          {state === "ready"
            ? "Connected"
            : state === "working"
              ? "Working"
              : state === "needed"
                ? "Not connected"
                : "Checking"}
        </span>
      )}
      {details ? (
        <ChevronRight
          aria-hidden
          className="size-4 shrink-0 self-center text-muted-foreground transition-transform motion-reduce:transition-none group-open:rotate-90"
        />
      ) : null}
    </>
  )
  const spacing = "flex items-start gap-3 px-4 py-3.5"
  return (
    <li className={details ? undefined : spacing}>
      {details ? (
        <details className="group">
          <summary
            className={cn(
              spacing,
              "cursor-pointer list-none transition-colors hover:bg-surface-2 focus-visible:bg-surface-2 motion-reduce:transition-none [&::-webkit-details-marker]:hidden",
            )}
          >
            {row}
          </summary>
          <div className="border-t border-line px-4 py-3.5">{details}</div>
        </details>
      ) : (
        row
      )}
    </li>
  )
}
