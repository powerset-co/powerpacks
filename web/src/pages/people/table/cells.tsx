import { label } from "@/lib/people/copy";
import type { Person } from "@/types/people";

const WARMTH_LEVELS = [1, 2, 3, 4] as const;

// Four bars and the number; a person never judged shows a dash.
export function WarmthCell({ value }: { value: number | null }) {
  if (value === null) return <div role="cell" className="warmth-cell c-warmth dim">—</div>;
  const on = Math.round(value);
  return (
    <div role="cell" className="warmth-cell c-warmth" title={`Warmth ${value.toFixed(1)} of 4`}>
      <span className="warmth-bar">
        {WARMTH_LEVELS.map((level) => <i key={level} className={level <= on ? "on" : ""} />)}
      </span>
      {value.toFixed(1)}
    </div>
  );
}

// The worth dot and word, and "You" when the owner decided it.
export function WorthCell({ row }: { row: Person }) {
  return (
    <div role="cell" className="worth c-worth" data-worth={row.worth}>
      <i className="dot" />
      {label("worth", row.worth || "unjudged")}{" "}
      <span className={`src ${row.worth_source}`}>{row.worth_source === "human" ? "You" : ""}</span>
    </div>
  );
}

export function NumberCell({ className, value }: { className: string; value: string }) {
  return <div role="cell" className={`cell-num ${className} right${value ? "" : " dim"}`}>{value || "—"}</div>;
}
