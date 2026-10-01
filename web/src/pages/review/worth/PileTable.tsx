import { useVirtualizer } from "@tanstack/react-virtual"
import { useCallback, useEffect, useRef } from "react"

import { must } from "@/lib/must"
import { LOAD_FAILED } from "@/lib/review/copy"
import type { DecisionRow } from "@/types/review"

import { EmptyPanel } from "../shared/EmptyPanel"
import { listLabel, LOADING_ROWS } from "./copy"
import { PileRow } from "./PileRow"
import type { Pile } from "./piles"
import { useOpenedRows } from "./useOpenedRows"
import type { PileMoves } from "./usePileMoves"
import { usePileTable, type PileRows } from "./usePileTable"

/** A collapsed row's height, until the row is measured. */
const ROW_ESTIMATE_PX = 61
/** Rows kept mounted past each edge of the viewport. */
const OVERSCAN_ROWS = 5
/** The next page is read once the viewport is this close to the end of the rows. */
const READ_AHEAD_PX = 120
const estimateSize = () => ROW_ESTIMATE_PX

function nearEnd({ scrollHeight, scrollTop, clientHeight }: HTMLElement): boolean {
  return scrollHeight - scrollTop - clientHeight < READ_AHEAD_PX
}

interface PileTableProps {
  /** The decided pile the tab shows. */
  pile: Pile
  /** The tabs' optimistic counts. */
  moves: PileMoves
}

// A decided pile's table: its rows once the first page is read, or why it could not be.
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
  return (
    <PileList
      pile={pile}
      table={table}
      reading={reading}
      leaving={leaving}
      onMore={() => void more()}
      onFlip={(row) => void flip(row)}
    />
  )
}

interface PileListProps {
  pile: Pile
  table: PileRows
  /** The next page is on its way. */
  reading: boolean
  /** Slugs whose flip is saving. */
  leaving: ReadonlySet<string>
  /** Read the next page (a no-op while one is on its way or every row is held). */
  onMore: () => void
  onFlip: (row: DecisionRow) => void
}

/**
 * rendering.py `render_decision_table` + reconcile_review.js `virtualizeDecisions`: the pile
 * scrolls inside its own box, and only the rows near the viewport are mounted, in the flow
 * between two spacers that stand in for the rest. Rows are measured (an opened row is
 * taller) and keyed by slug. The next page is read whenever the viewport is near the end of
 * the rows: on scroll, on resize, and a frame after the rows change (the first page, each
 * page after it, a flipped row leaving).
 */
function PileList({ pile, table, reading, leaving, onMore, onFlip }: PileListProps) {
  const { rows, total } = table
  const viewport = useRef<HTMLDivElement>(null)
  const opened = useOpenedRows()
  const getItemKey = useCallback((index: number) => must(rows[index]).person.slug, [rows])
  const virtualizer = useVirtualizer({
    count: rows.length,
    getScrollElement: () => viewport.current,
    estimateSize,
    getItemKey,
    overscan: OVERSCAN_ROWS,
  })
  const mounted = virtualizer.getVirtualItems()
  const above = mounted[0]?.start ?? 0
  const below = Math.max(0, virtualizer.getTotalSize() - (mounted.at(-1)?.end ?? 0))

  const readOn = () => {
    if (nearEnd(must(viewport.current, "the list"))) onMore()
  }
  // The observer and the frame outlive the render that set them up: they call the latest.
  const latest = useRef(readOn)
  useEffect(() => {
    latest.current = readOn
  })
  useEffect(() => {
    const resized = new ResizeObserver(() => latest.current())
    resized.observe(must(viewport.current, "the list"))
    return () => resized.disconnect()
  }, [])
  useEffect(() => {
    const frame = requestAnimationFrame(() => latest.current())
    return () => cancelAnimationFrame(frame)
  }, [rows])

  return (
    <div
      ref={viewport}
      className="decision-list"
      // A scrolling box the keyboard must be able to reach (LINT-WAIVERS.md W4).
      // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex -- W4
      tabIndex={0}
      aria-label={listLabel(pile)}
      onScroll={readOn}
    >
      <div className="decision-table" data-view={pile} data-total={total}>
        <div className="virtual-spacer" aria-hidden="true" style={{ height: above }} />
        {mounted.map(({ index, key }) => {
          const row = must(rows[index])
          const { slug } = row.person
          return (
            <PileRow
              key={key}
              row={row}
              pile={pile}
              index={index}
              measure={virtualizer.measureElement}
              open={opened.isOpen(slug)}
              details={opened.detailsOf(slug)}
              leaving={leaving.has(slug)}
              onToggle={(open) => opened.toggle(slug, open)}
              onFlip={() => onFlip(row)}
            />
          )
        })}
        <div className="virtual-spacer" aria-hidden="true" style={{ height: below }} />
      </div>
      <p className="decision-loading" role="status" hidden={!reading}>
        {LOADING_ROWS}
      </p>
    </div>
  )
}
