import { memo, type MouseEvent } from "react"

import { Avatar, SourcePills } from "@/components/shared"
import { avatarUrl } from "@/lib/api/people"
import { toChannels } from "@/lib/channels"
import { monthYear } from "@/lib/copy"
import { label, sentence } from "@/lib/people/copy"
import type { Person } from "@/types/people"

import { NumberCell, WarmthCell, WorthCell } from "./cells"

interface PersonRowProps {
  row: Person
  index: number
  selected: boolean
  focused: boolean
  open: boolean
  pending: boolean
  onOpen: (id: string, index: number) => void
  onSelect: (id: string) => void
}

// The checkbox keeps its own clicks: selecting never opens the drawer.
const stop = (event: MouseEvent) => event.stopPropagation()

export const PersonRow = memo(function PersonRow({
  row,
  index,
  selected,
  focused,
  open,
  pending,
  onOpen,
  onSelect,
}: PersonRowProps) {
  return (
    // Focus and keys live on the viewport (useKeyboard: j/k move, Enter opens); a row is not a tab stop.
    // eslint-disable-next-line jsx-a11y/click-events-have-key-events, jsx-a11y/interactive-supports-focus -- W3
    <div
      className="row focus-bar"
      role="row"
      data-id={row.parent_id}
      data-index={index}
      aria-selected={selected}
      data-focus={focused}
      data-open={open}
      data-pending={pending}
      onClick={() => onOpen(row.parent_id, index)}
    >
      <div role="cell" className="c-check check">
        <input
          type="checkbox"
          data-select
          aria-label={`Select ${row.name}`}
          checked={selected}
          onClick={stop}
          onChange={() => onSelect(row.parent_id)}
        />
      </div>
      <div role="cell" className="person c-person">
        <Avatar name={row.name} size={26} src={row.has_avatar ? avatarUrl(row.parent_id) : undefined} />
        <span className="who">
          <b>{row.name}</b>
        </span>
      </div>
      <SourcePills role="cell" className="sources c-sources" channels={toChannels(row.channels)} />
      <div role="cell" className={`why c-why${row.share_source === "human" ? " human" : ""}`}>
        {label("reason", row.reason)}
      </div>
      <div role="cell" className="rel c-rel">
        {sentence(row.relationship_kind)}
      </div>
      <WorthCell row={row} />
      <WarmthCell value={row.warmth} />
      <NumberCell className="c-last" value={row.last_interaction ? monthYear(row.last_interaction) : ""} />
      <NumberCell className="c-msgs" value={row.interactions ? row.interactions.toLocaleString() : ""} />
    </div>
  )
})
