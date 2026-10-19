import type { ReactNode } from "react"

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
}: {
  icon: ReactNode
  title: ReactNode
  why: ReactNode
  state: ConnectionState
  action?: { label: string; onClick: () => void; disabled?: boolean }
  // What the step says while it works or stopped: its line, a code to scan, a command.
  children?: ReactNode
}) {
  return (
    <li className="flex items-start gap-3 px-4 py-3.5">
      <span className="grid size-9 shrink-0 place-items-center rounded-[var(--radius-s)] bg-surface-2 text-foreground">
        {icon}
      </span>
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
          {state === "ready" ? <CheckIcon className="size-3.5" /> : <Spinner />}
          {state === "ready" ? "Connected" : state === "working" ? "Working" : "Checking"}
        </span>
      )}
    </li>
  )
}
