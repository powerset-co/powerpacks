import { useCallback, useRef, useState, type ReactNode } from "react"

import { EmptyState, VirtualRows, type VirtualRowsHandle } from "@/components/shared"
import { useListEntrance } from "@/hooks/useListEntrance"
import { EMPTY, toggled } from "@/lib/sets"
import type { PondCandidate, Tagged } from "@/types/searches"

import type { ResultRow as Result } from "../lib/ranking"
import { ROW_H } from "./columns"
import { ResultRow } from "./ResultRow"

export type ResultItem =
  { kind: "heading"; key: string; text: string } | { kind: "row"; key: string; result: Result }

interface ResultsTableProps {
  // Changes identity exactly when the rows do (a run, a pond); rows then rise in.
  items: readonly ResultItem[]
  ranked: boolean
  empty: ReactNode
  tags: Tagged | null | undefined
  rowActions: ((candidate: PondCandidate) => ReactNode) | undefined
}

const itemKey = (item: ResultItem) => item.key

// The virtualized people table. Rows are measured, so an expanded row grows in place; each
// row opens and closes on its own, so opening one never moves another out of view.
export function ResultsTable({ items, ranked, empty, tags, rowActions }: ResultsTableProps) {
  const [expanded, setExpanded] = useState<ReadonlySet<string>>(EMPTY)
  const rows = useRef<VirtualRowsHandle>(null)
  const count = items.filter((item) => item.kind === "row").length
  useListEntrance(() => rows.current?.element ?? null, items, count, ".results-heading, .result-row")

  const toggle = useCallback((key: string) => {
    setExpanded((current) => toggled(current, key))
  }, [])

  return (
    <section className="results" data-results aria-label="People">
      <div className="result-head" aria-hidden="true">
        <span className="result-head-main">
          <span>Person</span>
          <span>{ranked ? "Overall" : "Pond score"}</span>
          <span>Signals</span>
          <span>Sources</span>
          <span>Via</span>
          <span>Roles</span>
          <span />
        </span>
      </div>
      <VirtualRows
        handle={rows}
        className="results-viewport"
        data-results-viewport
        items={items}
        rowHeight={ROW_H}
        measure
        getKey={itemKey}
        renderRow={(item) =>
          item.kind === "heading" ? (
            <h3 className="results-heading">{item.text}</h3>
          ) : (
            <ResultRow
              result={item.result}
              ranked={ranked}
              expanded={expanded.has(item.key)}
              tags={tags?.assignments[item.result.row.person_id]}
              actions={rowActions?.(item.result.row)}
              onToggle={toggle}
            />
          )
        }
      />
      {count ? null : (
        <EmptyState className="results-empty" data-results-empty>
          {empty}
        </EmptyState>
      )}
    </section>
  )
}
