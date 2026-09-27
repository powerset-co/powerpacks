import { memo } from "react"

import { plural } from "@/lib/people/copy"
import type { SearchCard } from "@/types/searches"

import { runDate, statusText } from "../lib/copy"

interface RunItemProps {
  card: SearchCard
  selected: boolean
  highlighted: boolean
  onOpen: (runId: string) => void
}

// One saved search: title, company and status, people and date. The open run carries the
// rail's active look; the keyboard highlight is the table's focus bar.
export const RunItem = memo(function RunItem({ card, selected, highlighted, onOpen }: RunItemProps) {
  return (
    <button
      type="button"
      className="run-item"
      data-run-id={card.run_id}
      data-highlight={highlighted}
      aria-current={selected ? "page" : undefined}
      onClick={() => onOpen(card.run_id)}
    >
      <b className="run-item-title">{card.title}</b>
      <span className="run-item-meta">
        {card.company ? <span className="run-item-company">{card.company}</span> : null}
        <span data-status={card.status}>{statusText(card.status)}</span>
      </span>
      <span className="run-item-foot">
        <span>{plural(card.candidates, "person")}</span>
        <span>{runDate(card.created_at)}</span>
      </span>
    </button>
  )
})
