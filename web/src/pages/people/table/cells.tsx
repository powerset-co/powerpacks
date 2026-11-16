import { WarmthBar } from "@/components/shared"
import { label } from "@/lib/people/copy"
import type { Person } from "@/types/people"

// Four bars and the number; a person never judged shows a dash.
export function WarmthCell({ value }: { value: number | null }) {
  if (value === null)
    return (
      <div role="cell" className="warmth-cell c-warmth dim">
        —
      </div>
    )
  return (
    <div role="cell" className="warmth-cell c-warmth" title={`Warmth ${value.toFixed(1)} of 4`}>
      <WarmthBar value={value} />
      {value.toFixed(1)}
    </div>
  )
}

// The worth dot and word ("—" before any decision), and "You" when the owner decided it.
export function WorthCell({ row }: { row: Person }) {
  return (
    <div role="cell" className="worth c-worth" data-worth={row.worth}>
      <i className="dot" />
      {row.worth ? label("worth", row.worth) : "—"}{" "}
      <span className={`src ${row.worth_source}`}>{row.worth_source === "human" ? "You" : ""}</span>
    </div>
  )
}

export function NumberCell({ className, value }: { className: string; value: string }) {
  return (
    <div role="cell" className={`cell-num ${className} right${value ? "" : " dim"}`}>
      {value || "—"}
    </div>
  )
}
