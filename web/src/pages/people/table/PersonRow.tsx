import { memo, type MouseEvent } from "react";

import { Avatar, SourcePills } from "@/components/shared";
import { avatarUrl } from "@/lib/api/people";
import { formatDate, label, sentence } from "@/lib/people/copy";
import type { Person } from "@/types/people";

import { toChannels } from "../channels";
import { NumberCell, WarmthCell, WorthCell } from "./cells";

interface PersonRowProps {
  row: Person;
  index: number;
  selected: boolean;
  focused: boolean;
  open: boolean;
  pending: boolean;
  onOpen: (id: string, index: number) => void;
  onSelect: (id: string) => void;
}

// The checkbox's label takes its own clicks: selecting never opens the drawer.
const stop = (event: MouseEvent) => event.stopPropagation();

export const PersonRow = memo(function PersonRow({ row, index, selected, focused, open, pending, onOpen, onSelect }: PersonRowProps) {
  return (
    <div
      className="row"
      role="row"
      data-id={row.parent_id}
      data-index={index}
      aria-selected={selected}
      data-focus={focused}
      data-open={open}
      data-pending={pending}
      onClick={() => onOpen(row.parent_id, index)}
    >
      <div role="cell" className="c-check">
        <label className="check" onClick={stop}>
          <input type="checkbox" data-select aria-label={`Select ${row.name}`} checked={selected} onChange={() => onSelect(row.parent_id)} />
        </label>
      </div>
      <div role="cell" className="person c-person">
        <Avatar name={row.name} size={26} src={row.has_avatar ? avatarUrl(row.parent_id) : undefined} />
        <span className="who"><b>{row.name}</b></span>
      </div>
      <SourcePills role="cell" className="sources c-sources" channels={toChannels(row.channels)} />
      <div role="cell" className={`why c-why${row.share_source === "human" ? " human" : ""}`}>{label("reason", row.reason)}</div>
      <div role="cell" className="rel c-rel">{sentence(row.relationship_kind)}</div>
      <WorthCell row={row} />
      <WarmthCell value={row.warmth} />
      <NumberCell className="c-last" value={row.last_interaction ? formatDate(row.last_interaction) : ""} />
      <NumberCell className="c-msgs" value={row.interactions ? row.interactions.toLocaleString() : ""} />
    </div>
  );
});
