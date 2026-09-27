import type { ReactNode } from "react"

import type { ResultRow, ResultSection, Results } from "@/lib/searches/ranking"
import type { SearchResult } from "@/types/searches"

import { PondChain } from "./PondChain"
import { ResultsTable, type ResultItem } from "./ResultsTable"
import { RunHeader } from "./RunHeader"
import { TeamPanel } from "./TeamPanel"

export interface SearchRunProps {
  search: SearchResult
  // The run's status from its catalog card.
  status?: string
  mode: Results["mode"]
  // Without a screen the pond chain picks the table's pond.
  pondAt: number
  onPond: (index: number) => void
  // The panel's people before filtering, and the sections the filters keep.
  people: number
  sections: readonly ResultSection[]
  filtered: boolean
  // Taste, Suggested pin and Team similarity labels on each row.
  labels: boolean
  // The run's controls (web/README.md "Searches").
  toolbar?: ReactNode
  headerActions?: ReactNode
  rowActions?: (result: ResultRow) => ReactNode
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

// One saved run: header, pond chain, the toolbar, the people the filters keep, the team.
export function SearchRun(props: SearchRunProps) {
  const { search, mode, pondAt, sections, filtered } = props
  return (
    <div className="search-run" data-search-run>
      <RunHeader search={search} status={props.status} people={props.people} actions={props.headerActions} />
      <PondChain ponds={search.ponds} selected={mode === "ponds" ? pondAt : null} onSelect={props.onPond} />
      {props.toolbar ? (
        <div className="run-toolbar" data-run-toolbar>
          {props.toolbar}
        </div>
      ) : null}
      <ResultsTable
        items={tableItems(sections)}
        ranked={mode === "ranked"}
        labels={props.labels}
        empty={emptyText(search, mode, pondAt, filtered)}
        rowActions={props.rowActions}
      />
      <TeamPanel search={search} />
    </div>
  )
}
