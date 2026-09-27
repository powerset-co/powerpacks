import { useCallback, useEffect, useRef, useState } from "react"

import { Button } from "@/components/ui/button"
import { usePresence } from "@/hooks/usePresence"
import type { OperatorOption } from "@/lib/searches/filters"
import { toggled } from "@/lib/sets"
import { cn } from "@/lib/utils"

import { Appear } from "./Appear"
import { OperatorInitials } from "./OperatorInitials"
import { GROUP_LABEL, PANEL } from "./styles"
import { type DismissBy, useDismiss } from "./useDismiss"

interface OperatorPickerProps {
  operators: readonly OperatorOption[]
  selected: ReadonlySet<string>
  onChange: (selected: ReadonlySet<string>) => void
}

// "Operators", a removable initials chip per chosen operator, and "+" opening the list.
export function OperatorPicker({ operators, selected, onChange }: OperatorPickerProps) {
  const [open, setOpen] = useState(false)
  const anchor = useRef<HTMLButtonElement>(null)
  const panel = useRef<HTMLDivElement>(null)
  const close = useCallback((by: DismissBy) => {
    setOpen(false)
    if (by === "escape") anchor.current?.focus()
  }, [])
  useDismiss(open, panel, anchor, close)
  const presence = usePresence(open ? true : null)

  useEffect(() => {
    if (open) panel.current?.querySelector("input")?.focus()
  }, [open])

  if (!operators.length) return null
  return (
    <span className="relative inline-flex items-center gap-1.5" role="group" aria-label="Operators">
      <span className={GROUP_LABEL}>Operators</span>
      {operators.map((operator) => (
        <Appear key={operator.id} show={selected.has(operator.id)}>
          <button
            type="button"
            aria-label={`Remove ${operator.name}`}
            title={operator.name}
            className="inline-flex cursor-pointer rounded-full border border-primary bg-primary-soft p-0.5 transition-[border-color,background-color] duration-fast ease-out hover:border-line-strong"
            onClick={() => {
              onChange(toggled(selected, operator.id, false))
              anchor.current?.focus()
            }}
          >
            <OperatorInitials name={operator.name} />
          </button>
        </Appear>
      ))}
      <Button
        ref={anchor}
        variant="ghost"
        size="sm"
        shape="pill"
        className="size-7 p-0"
        aria-label="Add operator"
        title="Filter by operator"
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
      >
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true">
          <path d="M12 5v14M5 12h14" />
        </svg>
      </Button>
      {presence.mounted ? (
        <div
          ref={panel}
          role="group"
          aria-label="Choose operators"
          data-open={presence.open}
          onTransitionEnd={presence.onTransitionEnd}
          className={cn(PANEL, "rise absolute left-0 top-full mt-1.5 w-64")}
        >
          <strong className="text-xs">Filter by operator</strong>
          <small className="mb-1 text-[11px] text-muted-foreground">
            Matches anyone connected to a selected operator.
          </small>
          {operators.map((operator) => (
            <label
              key={operator.id}
              className="flex cursor-pointer items-center gap-2 rounded-sm px-1.5 py-1 transition-colors duration-fast ease-out hover:bg-secondary"
            >
              <input
                type="checkbox"
                className="accent-[var(--primary)]"
                checked={selected.has(operator.id)}
                onChange={(event) => onChange(toggled(selected, operator.id, event.target.checked))}
              />
              <OperatorInitials name={operator.name} />
              <span>{operator.name}</span>
            </label>
          ))}
        </div>
      ) : null}
    </span>
  )
}
