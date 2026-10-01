import { LOAD_FAILED } from "@/lib/review/copy"

import { EmptyPanel } from "../shared/EmptyPanel"
import { showMoreRows } from "./copy"
import { PileRow } from "./PileRow"
import type { Pile } from "./piles"
import type { PileMoves } from "./usePileMoves"
import { usePileTable } from "./usePileTable"

interface PileTableProps {
  /** The decided pile the tab shows. */
  pile: Pile
  /** The tabs' optimistic counts. */
  moves: PileMoves
}

// rendering.py `render_decision_table`: one page of a decided pile's rows, then "Show more
// (N left)" while the pile holds more than the table shows.
export function PileTable({ pile, moves }: PileTableProps) {
  const { table, failure, reading, leaving, more, flip } = usePileTable(pile, moves)
  if (failure !== null) {
    return (
      <EmptyPanel title={LOAD_FAILED}>
        <p>{failure}</p>
      </EmptyPanel>
    )
  }
  if (!table) return null

  const { rows, total } = table
  const left = Math.max(0, total - rows.length)
  return (
    <>
      <div className="decision-table" data-view={pile}>
        {rows.map((row) => (
          <PileRow
            key={row.person.parent_id}
            row={row}
            pile={pile}
            leaving={leaving.has(row.person.parent_id)}
            onFlip={() => void flip(row)}
          />
        ))}
      </div>
      {left ? (
        <button
          className="button button-outline table-more"
          type="button"
          disabled={reading}
          onClick={() => void more(rows.length)}
        >
          {showMoreRows(left)}
        </button>
      ) : null}
    </>
  )
}
