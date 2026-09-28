import { useMemo, type ReactNode } from "react"

import type { ResultSection, Results } from "@/lib/searches/ranking"
import type { SearchResult } from "@/types/searches"

import { PondChain } from "./PondChain"
import { ResultsTable, type ResultItem } from "./ResultsTable"
import type { RowContext } from "./RowActions"
import { RunHeader } from "./RunHeader"

export interface SearchRunProps {
  search: SearchResult
  // The run's status from its catalog card; none until the catalog loads.
  status: string | undefined
  mode: Results["mode"]
  // Without a screen the pond chain picks the table's pond.
  pondAt: number
  onPond: (index: number) => void
  // With a screen: the pond the list is narrowed to, or null for every pond.
  pondOnly: number | null
  onPondOnly: (index: number) => void
  // The panel's people before filtering, and the sections the filters keep.
  people: number
  sections: readonly ResultSection[]
  filtered: boolean
  // Taste, Suggested pin and Team similarity labels on each row.
  labels: boolean
  // The run's controls (web/README.md "Searches").
  toolbar: ReactNode
  headerActions: ReactNode
  rowContext: RowContext
}

function tableItems(sections: readonly ResultSection[]): ResultItem[] {
  return sections.flatMap((section) => [
    ...(section.heading
      ? [{ kind: "heading" as const, key: `heading:${section.heading}`, text: section.heading }]
      : []),
    ...section.rows.map((result) => ({ kind: "row" as const, key: result.key, result })),
  ])
}

function emptyText(search: SearchResult, mode: Results["mode"], pondAt: number, filtered: boolean): string {
  if (filtered) return "No one here matches these filters."
  if (mode === "unavailable") return "Screening scores are unavailable for this run."
  const pond = search.ponds[pondAt]
  return pond
    ? `0 of ${pond.result_count.toLocaleString()} people this pond found scored 0.7 or higher, so none were kept.`
    : "This run has no ponds yet."
}

// One saved run: header (with the job description and the team), pond chain, the toolbar,
// the people the filters keep.
export function SearchRun(props: SearchRunProps) {
  const { search, mode, pondAt, sections, filtered } = props
  // One identity per filtered list: the table's row keys and measurements follow it.
  const items = useMemo(() => tableItems(sections), [sections])
  return (
    <div className="search-run" data-search-run>
      <RunHeader search={search} status={props.status} people={props.people} actions={props.headerActions} />
      <PondChain
        ponds={search.ponds}
        selected={mode === "ponds" ? pondAt : props.pondOnly}
        onSelect={mode === "ponds" ? props.onPond : props.onPondOnly}
      />
      <div className="run-toolbar" data-run-toolbar>
        {props.toolbar}
      </div>
      <ResultsTable
        items={items}
        ranked={mode === "ranked"}
        labels={props.labels}
        empty={emptyText(search, mode, pondAt, filtered)}
        rowContext={props.rowContext}
      />
    </div>
  )
}
