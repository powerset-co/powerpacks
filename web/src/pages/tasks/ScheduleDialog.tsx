import { useId, useState, type KeyboardEvent } from "react"

import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogBody,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { cn } from "@/lib/utils"
import type { ScheduleSettings } from "@/types/tasks"

// The refresh task's schedule, as packs/ingestion/primitives/refresh/tasks.py Schedule allows
// it: daily, weekdays or one weekday, at an HH:MM in this computer's timezone. Every control is
// the app's own, so the dialog reads the same in the desktop webview as in a browser; the native
// select and time inputs did not.

const CADENCES: readonly (readonly [ScheduleSettings["cadence"], string])[] = [
  ["daily", "Daily"],
  ["weekdays", "Weekdays"],
  ["weekly", "Weekly"],
]
const DAYS: readonly (readonly [string, string, string])[] = [
  ["MO", "Mon", "Monday"],
  ["TU", "Tue", "Tuesday"],
  ["WE", "Wed", "Wednesday"],
  ["TH", "Thu", "Thursday"],
  ["FR", "Fri", "Friday"],
  ["SA", "Sat", "Saturday"],
  ["SU", "Sun", "Sunday"],
]

export interface ScheduleDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  title: string
  submitLabel: string
  /** The install already has a schedule the page could not read; saving replaces it. */
  replacesCustom: boolean
  schedule: ScheduleSettings
  onSchedule: (next: ScheduleSettings) => void
  busy: boolean
  onSubmit: () => void
}

export function ScheduleDialog({
  open,
  onOpenChange,
  title,
  submitLabel,
  replacesCustom,
  schedule,
  onSchedule,
  busy,
  onSubmit,
}: ScheduleDialogProps) {
  const ids = { repeat: useId(), day: useId(), time: useId() }
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="gap-5 p-6">
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          <DialogDescription>
            Runs on this computer, in its time zone ({schedule.timezone.replaceAll("_", " ")}).
          </DialogDescription>
        </DialogHeader>
        <form
          className="flex min-h-0 flex-col gap-5"
          onSubmit={(event) => {
            event.preventDefault()
            onSubmit()
          }}
        >
          <DialogBody className="grid grid-cols-[56px_minmax(0,1fr)] items-center gap-x-4 gap-y-4 text-xs">
            <span id={ids.repeat} className="text-muted-foreground">
              Repeat
            </span>
            <Segmented
              labelledBy={ids.repeat}
              options={CADENCES}
              value={schedule.cadence}
              disabled={busy}
              onChange={(cadence) => onSchedule({ ...schedule, cadence })}
            />
            {schedule.cadence === "weekly" && (
              <>
                <span id={ids.day} className="text-muted-foreground">
                  Day
                </span>
                <div role="radiogroup" aria-labelledby={ids.day} className="flex flex-wrap gap-1.5">
                  {DAYS.map(([value, short, long]) => (
                    <button
                      key={value}
                      type="button"
                      role="radio"
                      aria-checked={schedule.day === value}
                      aria-label={long}
                      disabled={busy}
                      onClick={() => onSchedule({ ...schedule, day: value })}
                      className="min-h-7 cursor-pointer rounded-full border border-border bg-transparent px-2.5 text-[11.5px] font-semibold leading-none text-muted-foreground transition-[color,border-color,background-color] duration-fast ease-out hover:border-line-strong hover:text-foreground aria-checked:border-primary aria-checked:bg-primary-soft aria-checked:text-foreground disabled:cursor-default disabled:opacity-40"
                    >
                      {short}
                    </button>
                  ))}
                </div>
              </>
            )}
            <span id={ids.time} className="text-muted-foreground">
              Time
            </span>
            <TimeField
              labelledBy={ids.time}
              value={schedule.time}
              disabled={busy}
              onChange={(time) => onSchedule({ ...schedule, time })}
            />
          </DialogBody>
          {replacesCustom && (
            <p className="m-0 text-xs text-muted-foreground">
              The current schedule was set outside this page; saving replaces it with this one.
            </p>
          )}
          <DialogFooter>
            <DialogClose asChild>
              <Button type="button" variant="ghost">
                Cancel
              </Button>
            </DialogClose>
            <Button type="submit" variant="primary" disabled={busy}>
              {submitLabel}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

interface SegmentedProps<T extends string> {
  labelledBy?: string
  label?: string
  options: readonly (readonly [T, string])[]
  value: T
  disabled?: boolean
  onChange: (value: T) => void
}

/** A macOS-style segmented control: one raised segment, arrow keys move between them. */
function Segmented<T extends string>({
  labelledBy,
  label,
  options,
  value,
  disabled,
  onChange,
}: SegmentedProps<T>) {
  const step = (event: KeyboardEvent<HTMLButtonElement>) => {
    const delta =
      event.key === "ArrowRight" || event.key === "ArrowDown"
        ? 1
        : event.key === "ArrowLeft" || event.key === "ArrowUp"
          ? -1
          : 0
    if (delta === 0) return
    event.preventDefault()
    const index = options.findIndex(([key]) => key === value)
    const next = options[(index + delta + options.length) % options.length]?.[0]
    if (next === undefined) return
    onChange(next)
    event.currentTarget.parentElement?.querySelector<HTMLButtonElement>(`[data-value="${next}"]`)?.focus()
  }
  return (
    <div
      role="radiogroup"
      aria-labelledby={labelledBy}
      aria-label={label}
      className="inline-flex w-fit gap-0.5 rounded-[var(--radius-s)] border border-line bg-background p-0.5"
    >
      {options.map(([key, text]) => (
        <button
          key={key}
          type="button"
          role="radio"
          aria-checked={key === value}
          data-value={key}
          tabIndex={key === value ? 0 : -1}
          disabled={disabled}
          onClick={() => onChange(key)}
          onKeyDown={step}
          className="min-h-7 cursor-pointer rounded-[4px] border-0 bg-transparent px-3 text-xs font-semibold leading-none text-muted-foreground transition-[color,background-color] duration-fast ease-out hover:text-foreground aria-checked:bg-surface-2 aria-checked:text-foreground aria-checked:shadow-1 disabled:cursor-default disabled:opacity-40 focus-visible:outline-offset-[-2px]"
        >
          {text}
        </button>
      ))}
    </div>
  )
}

interface TimeFieldProps {
  labelledBy: string
  /** HH:MM, 24-hour. */
  value: string
  disabled?: boolean
  onChange: (value: string) => void
}

/** Hour and minute as two typed digit cells plus an AM/PM switch; arrow keys step each cell. */
function TimeField({ labelledBy, value, disabled, onChange }: TimeFieldProps) {
  const [hour24 = 0, minute = 0] = value.split(":").map(Number)
  const pm = hour24 >= 12
  const hour = hour24 % 12 || 12
  const set = (nextHour: number, nextMinute: number, nextPm: boolean) =>
    onChange(`${pad((nextHour % 12) + (nextPm ? 12 : 0))}:${pad(nextMinute)}`)
  return (
    <div role="group" aria-labelledby={labelledBy} className="flex flex-wrap items-center gap-2">
      <div className="inline-flex items-center gap-px rounded-[var(--radius-s)] border border-line bg-background p-0.5 px-1.5 tabular-nums focus-within:border-line-strong">
        <DigitCell
          label="Hour"
          value={hour}
          min={1}
          max={12}
          disabled={disabled}
          onChange={(n) => set(n, minute, pm)}
        />
        <span aria-hidden="true" className="text-muted-foreground">
          :
        </span>
        <DigitCell
          label="Minute"
          value={minute}
          min={0}
          max={59}
          padded
          disabled={disabled}
          onChange={(n) => set(hour, n, pm)}
        />
      </div>
      <Segmented
        label="AM or PM"
        options={[
          ["AM", "AM"],
          ["PM", "PM"],
        ]}
        value={pm ? "PM" : "AM"}
        disabled={disabled}
        onChange={(half) => set(hour, minute, half === "PM")}
      />
    </div>
  )
}

interface DigitCellProps {
  label: string
  value: number
  min: number
  max: number
  padded?: boolean
  disabled?: boolean
  onChange: (value: number) => void
}

function DigitCell({ label, value, min, max, padded = false, disabled, onChange }: DigitCellProps) {
  // What the user has typed so far; null shows the value. A valid number commits as it is typed,
  // so "3" then "30" lands on 30; leaving the cell with junk in it restores the value.
  const [draft, setDraft] = useState<string | null>(null)
  const shown = draft ?? (padded ? pad(value) : String(value))
  return (
    <input
      aria-label={label}
      inputMode="numeric"
      autoComplete="off"
      disabled={disabled}
      value={shown}
      onFocus={(event) => event.currentTarget.select()}
      onBlur={() => setDraft(null)}
      onChange={(event) => {
        const digits = event.target.value.replace(/\D/g, "").slice(0, 2)
        setDraft(digits)
        const next = Number(digits)
        if (digits !== "" && next >= min && next <= max) onChange(next)
      }}
      onKeyDown={(event) => {
        const delta = event.key === "ArrowUp" ? 1 : event.key === "ArrowDown" ? -1 : 0
        if (delta === 0) return
        event.preventDefault()
        setDraft(null)
        const span = max - min + 1
        onChange(min + ((value - min + delta + span) % span))
      }}
      className={cn(
        "h-7 w-[2.4ch] rounded-[4px] border-0 bg-transparent p-0 text-center text-xs font-semibold text-foreground outline-none selection:bg-primary-soft",
        "focus:bg-primary-soft focus-visible:outline-none",
      )}
    />
  )
}

function pad(n: number): string {
  return String(n).padStart(2, "0")
}
