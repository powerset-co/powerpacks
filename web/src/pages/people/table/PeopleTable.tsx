import { forwardRef, useImperativeHandle, useRef, type ReactNode } from "react"

import { EmptyState, VirtualRows, type VirtualRowsHandle } from "@/components/shared"
import { useListEntrance } from "@/hooks/useListEntrance"
import type { Sort, SortKey } from "@/lib/people/facets"
import type { PeopleView } from "@/lib/people/view"
import { personKey, type Person } from "@/types/people"

import { ROW_H } from "./columns"
import { GridHead } from "./GridHead"
import { PersonRow } from "./PersonRow"
import "../styles/table.css"

interface PeopleTableProps {
  // The view's rows in order; `null` where its slice has not loaded yet.
  matching: readonly (Person | null)[]
  sort: Sort
  selected: ReadonlySet<string>
  allSelected: boolean
  someSelected: boolean
  focus: number
  openId: string | null
  pending: ReadonlySet<string>
  // Changes identity exactly when the view (tab, filters, search, sort) changes.
  view: PeopleView
  empty: ReactNode
  onSort: (key: SortKey) => void
  onSelectAll: () => void
  onSelect: (id: string) => void
  onOpen: (id: string, index: number) => void
  onRange: (start: number, end: number) => void
}

export type PeopleTableHandle = Pick<VirtualRowsHandle, "scrollToIndex" | "scrollToOffset">

// A loaded row keeps its person's key; a row still loading holds its place.
const slotKey = (row: Person | null, index: number): string => (row ? personKey(row) : `loading-${index}`)

export const PeopleTable = forwardRef<PeopleTableHandle, PeopleTableProps>((props, ref) => {
  const { matching, selected, focus, openId, pending, onOpen, onSelect } = props
  const rows = useRef<VirtualRowsHandle>(null)
  useImperativeHandle(
    ref,
    () => ({
      scrollToIndex: (index, options) => rows.current?.scrollToIndex(index, options),
      scrollToOffset: (offset, options) => rows.current?.scrollToOffset(offset, options),
    }),
    [],
  )
  useListEntrance(() => rows.current?.element ?? null, props.view, matching.length, ".row")

  return (
    <section
      className="people-grid"
      data-grid
      role="table"
      aria-label="People"
      aria-rowcount={matching.length + 1}
    >
      <GridHead
        sort={props.sort}
        matching={matching.length}
        allSelected={props.allSelected}
        someSelected={props.someSelected}
        onSort={props.onSort}
        onSelectAll={props.onSelectAll}
      />
      <VirtualRows
        handle={rows}
        className="grid-viewport"
        data-viewport
        tabIndex={0}
        role="rowgroup"
        items={matching}
        rowHeight={ROW_H}
        getKey={slotKey}
        onRange={props.onRange}
        renderRow={(row, index) =>
          row === null ? (
            <div className="row row-loading" role="row" aria-busy="true" />
          ) : (
            <PersonRow
              row={row}
              index={index}
              selected={selected.has(row.parent_id)}
              focused={index === focus}
              open={row.parent_id === openId}
              pending={pending.has(row.parent_id)}
              onOpen={onOpen}
              onSelect={onSelect}
            />
          )
        }
      />
      {matching.length ? null : <EmptyState data-empty>{props.empty}</EmptyState>}
    </section>
  )
})
PeopleTable.displayName = "PeopleTable"
