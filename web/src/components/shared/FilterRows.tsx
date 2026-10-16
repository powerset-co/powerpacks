// A search's filters as rows: a label on the left, the values on the right as chips or text.
// The chat card shows them while it asks (and edits them in place); a saved run shows the ones it
// ran with over its people.

import type { ReactNode } from "react"

import { type Field, type Values, names, scalar } from "@/lib/agent/searchFields"
import type { FilterValue, Trait } from "@/types/searches"

export const CHIP = "rounded-full border border-line px-2.5 py-1 text-[11.5px] font-semibold leading-snug"

/** A label on the left, the field's values on the right. */
export function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="grid grid-cols-[96px_minmax(0,1fr)] items-start gap-x-2 gap-y-1 max-[480px]:grid-cols-1">
      <span className="pt-1 text-[11px] font-semibold uppercase tracking-[.04em] text-muted-foreground">
        {label}
      </span>
      <div className="flex min-w-0 flex-wrap items-center gap-1.5">{children}</div>
    </div>
  )
}

/** A min and a max as one line; an unset bound reads "any". */
function range(low: string, high: string): string {
  return `${low === "" ? "any" : low} – ${high === "" ? "any" : high}`
}

/** One field's values, read-only: chips for names (a location with its kind), text for the rest. */
export function ReadRow({ field, values }: { field: Field; values: Values }) {
  const control = field.control
  switch (control.kind) {
    case "chips":
      return (
        <Row label={field.label}>
          {names(values.get(control.key)).map((name, index) => (
            <span key={index} className={CHIP}>
              {name}
            </span>
          ))}
        </Row>
      )
    case "kinds":
      return (
        <Row label={field.label}>
          {Object.entries(control.keys).flatMap(([key, kind]) =>
            names(values.get(key)).map((name) => (
              <span key={`${key}:${name}`} className={CHIP}>
                {name}
                <span className="text-faint"> · {kind}</span>
              </span>
            )),
          )}
        </Row>
      )
    case "text":
      return (
        <Row label={field.label}>
          <span className="text-[12.5px]">{scalar(values.get(control.key))}</span>
        </Row>
      )
    case "pair": {
      // Money reads in millions; the payload holds dollars.
      const scale = control.scale ?? 1
      const shown = (value: FilterValue | null | undefined) =>
        typeof value === "number" ? String(value / scale) : scalar(value)
      const [low, high] = control.keys.map((key) => shown(values.get(key)))
      return (
        <Row label={field.label}>
          <span className="text-[12.5px]">{range(low ?? "", high ?? "")}</span>
        </Row>
      )
    }
    case "select-pair": {
      const [low, high] = control.keys.map((key) => scalar(values.get(key)))
      return (
        <Row label={field.label}>
          <span className="text-[12.5px]">{range(low ?? "", high ?? "")}</span>
        </Row>
      )
    }
    case "toggle":
      return (
        <Row label={field.label}>
          <span className="text-[12.5px]">{values.get(control.key) === true ? "yes" : "no"}</span>
        </Row>
      )
  }
}

/** The traits as chips: each value with its temporal. */
export function TraitChips({ traits }: { traits: readonly Pick<Trait, "value" | "temporal">[] }) {
  return (
    <>
      {traits.map((trait, index) => (
        <span key={index} className={CHIP}>
          {trait.value} <span className="text-faint">({trait.temporal})</span>
        </span>
      ))}
    </>
  )
}
