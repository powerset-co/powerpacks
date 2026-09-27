import { useCallback, useMemo, useRef, useState, type ReactNode } from "react"

import { EmptyState, VirtualRows, type VirtualRowsHandle } from "@/components/shared"
import { useListEntrance } from "@/hooks/useListEntrance"
import type { ResultRow as Result } from "@/lib/searches/ranking"
import { EMPTY, toggled } from "@/lib/sets"

import { useResultKeys } from "../hooks/useResultKeys"
import { ResultRow } from "./ResultRow"
import type { RowContext } from "./RowActions"
import { ROW_H } from "./columns"

export type ResultItem =
  { kind: "heading"; key: string; text: string } | { kind: "row"; key: string; result: Result }

interface ResultsTableProps {
  items: readonly ResultItem[]
  ranked: boolean
  labels: boolean
  empty: ReactNode
  rowContext: RowContext
}

const itemKey = (item: ResultItem) => item.key

// The virtualized people table. Rows are measured, so an expanded row grows in place; each
// row opens and closes on its own, so opening one never moves another out of view. The keys
// (hooks/useResultKeys) drive a focused row; t and s press that row's tag and score buttons.
export function ResultsTable({ items, ranked, labels, empty, rowContext }: ResultsTableProps) {
  const [expanded, setExpanded] = useState<ReadonlySet<string>>(EMPTY)
  const [focus, setFocus] = useState<string | null>(null)
  const rows = useRef<VirtualRowsHandle>(null)
  const count = items.filter((item) => item.kind === "row").length
  // Rows rise in when the list changes (a run, a pond, a filter), not when a row's tags or score do.
  const signature = items.map(itemKey).join("\n")
  const list = useMemo(() => ({ signature }), [signature])
  useListEntrance(() => rows.current?.element ?? null, list, count, ".results-heading, .result-row")

  const toggle = useCallback((key: string) => {
    setExpanded((current) => toggled(current, key))
  }, [])

  const focused = items.findIndex((item) => item.key === focus)
  const press = (action: "tag" | "score") => {
    const key = items[focused]?.key
    if (key === undefined) return false
    const row = [...(rows.current?.element?.querySelectorAll<HTMLElement>("[data-result-key]") ?? [])].find(
      (element) => element.dataset.resultKey === key,
    )
    row?.querySelector<HTMLElement>(`[data-row-action="${action}"]`)?.click()
    return true
  }

  useResultKeys({
    move: (step) => {
      const rowIndexes = items.flatMap((item, index) => (item.kind === "row" ? [index] : []))
      const at = rowIndexes.indexOf(focused)
      const next = rowIndexes[at < 0 ? 0 : Math.min(rowIndexes.length - 1, Math.max(0, at + step))]
      if (next === undefined) return
      setFocus(items[next]?.key ?? null)
      rows.current?.scrollToIndex(next, { align: "auto" })
    },
    toggle: () => {
      const key = items[focused]?.key
      if (key !== undefined) toggle(key)
      return key !== undefined
    },
    tag: () => press("tag"),
    score: () => press("score"),
  })

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
        <span className="result-actions" />
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
              labels={labels}
              expanded={expanded.has(item.key)}
              focused={item.key === focus}
              context={rowContext}
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
